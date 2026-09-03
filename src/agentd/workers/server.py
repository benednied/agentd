"""Persistent authenticated asyncio worker server."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Iterable
from contextlib import suppress

from agentd.harness.protocol import HarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.workers.errors import WorkerOperationError, WorkerProtocolError
from agentd.workers.execution import ExecutionService
from agentd.workers.journal import OperationJournal
from agentd.workers.remote_protocol import (
    MAX_FRAME_SIZE,
    Envelope,
    MessageKind,
    PSKAuthenticator,
    error_response,
    make_response,
    read_frame,
    write_frame,
)


class WorkerServer:
    """Serve typed run operations from one worker node.

    TLS is mandatory unless a caller explicitly opts into loopback-only
    plaintext for tests.  Every request is authenticated independently, so a
    reconnect does not create a new authorization boundary.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        node_id: str,
        session_epoch: str,
        secret: bytes,
        drivers: DriverRegistry | Iterable[HarnessDriver] | None = None,
        journal: OperationJournal | None = None,
        ssl_context: ssl.SSLContext | None = None,
        allow_insecure_loopback: bool = False,
        max_frame_size: int = MAX_FRAME_SIZE,
        max_connections: int = 32,
        max_inflight: int = 16,
        frame_timeout_seconds: float = 30.0,
        request_timeout_seconds: float = 30.0,
        tls_handshake_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self._validate_endpoint(host, allow_insecure_loopback, ssl_context)
        if not 0 <= port <= 65_535:
            raise ValueError("port must be between 0 and 65535")
        if max_frame_size <= 0 or max_frame_size > MAX_FRAME_SIZE:
            raise ValueError("max_frame_size is outside the protocol limit")
        if max_connections <= 0 or max_inflight <= 0:
            raise ValueError("worker concurrency limits must be positive")
        if frame_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("frame and request timeouts must be positive")
        if tls_handshake_timeout_seconds <= 0:
            raise ValueError("tls_handshake_timeout_seconds must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self.host = host
        self.port = port
        self.node_id = node_id
        self.session_epoch = session_epoch
        self._secret = bytes(secret)
        self._authenticator = PSKAuthenticator(secret)
        self._ssl_context = ssl_context
        self._allow_insecure_loopback = allow_insecure_loopback
        self._max_frame_size = max_frame_size
        self._connection_limit = asyncio.Semaphore(max_connections)
        self._inflight = asyncio.Semaphore(max_inflight)
        self._frame_timeout = frame_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._tls_handshake_timeout = tls_handshake_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        if journal is None:
            raise ValueError("a durable operation journal is required")
        if journal.path == ":memory:":
            raise ValueError("WorkerServer requires a file-backed operation journal")
        if journal.node_id != node_id or journal.session_epoch != session_epoch:
            raise ValueError("operation journal identity must match the worker server")
        self._journal = journal
        if drivers is None:
            registry = DriverRegistry()
        elif isinstance(drivers, DriverRegistry):
            registry = drivers
        else:
            registry = DriverRegistry(drivers)
        self._service = ExecutionService(registry, journal)
        self._server: asyncio.Server | None = None
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._client_tasks: set[asyncio.Task[object]] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @property
    def address(self) -> tuple[str, int] | None:
        server = self._server
        if server is None:
            return None
        sockets = server.sockets
        if not sockets:
            return None
        sockname = sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

    async def start(self) -> tuple[str, int]:
        async with self._lifecycle_lock:
            if self._closed or self._closing:
                raise RuntimeError("worker server is closed")
            if self._server is not None:
                address = self.address
                if address is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError("worker server has no listening socket")
                return address
            self._server = await asyncio.start_server(
                self._handle_client,
                self.host,
                self.port,
                ssl=self._ssl_context,
                ssl_handshake_timeout=(
                    self._tls_handshake_timeout
                    if self._ssl_context is not None
                    else None
                ),
                limit=self._max_frame_size + 4,
            )
            address = self.address
            if address is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("worker server did not expose a listening socket")
            return address

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            server, self._server = self._server, None
            if server is not None:
                server.close()

            # Closing established writers wakes both idle readers and clients
            # waiting for a response. The callbacks themselves are cancelled
            # below so a connection cannot outlive the server lifecycle.
            for writer in tuple(self._client_writers):
                writer.close()
            await self._cancel_tasks(tuple(self._client_tasks))
            await self._cancel_tasks(tuple(self._background_tasks))
            self._client_tasks = {
                task for task in self._client_tasks if not task.done()
            }
            self._background_tasks = {
                task for task in self._background_tasks if not task.done()
            }
            if self._client_tasks or self._background_tasks:
                # Do not close SQLite while an uncooperative driver could
                # still be using it. A caller can retry close after the
                # bounded timeout and the listener/connections are already
                # shut down.
                raise TimeoutError("worker server shutdown timed out")
            if server is not None:
                await server.wait_closed()
            self._journal.close()
            self._closed = True

    async def _cancel_tasks(self, tasks: tuple[asyncio.Task[object], ...]) -> None:
        current = asyncio.current_task()
        pending = {task for task in tasks if task is not current and not task.done()}
        for task in pending:
            task.cancel()
        if not pending:
            return
        await asyncio.wait(pending, timeout=self._shutdown_timeout)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client_task = asyncio.current_task()
        if client_task is not None:
            self._client_tasks.add(client_task)
        self._client_writers.add(writer)
        acquired = False
        try:
            if self._closing or self._connection_limit.locked():
                return
            await self._connection_limit.acquire()
            acquired = True
            if self._closing:
                return
            while not reader.at_eof():
                try:
                    request = await asyncio.wait_for(
                        read_frame(
                            reader,
                            max_frame_size=self._max_frame_size,
                        ),
                        timeout=self._frame_timeout,
                    )
                    self._authorize(request)
                except (EOFError, asyncio.IncompleteReadError, TimeoutError):
                    break
                except (WorkerProtocolError, ValueError):
                    # Authentication and framing errors close the connection
                    # without an oracle response.  The peer must reconnect
                    # with a fresh nonce and a valid envelope.
                    break

                operation_task = asyncio.create_task(self._execute(request))
                self._background_tasks.add(operation_task)
                operation_task.add_done_callback(self._background_tasks.discard)
                try:
                    response = await asyncio.wait_for(
                        asyncio.shield(operation_task),
                        timeout=self._request_timeout,
                    )
                except TimeoutError:
                    # Leave the operation task alive so a later retry can
                    # replay its journaled response; no ambiguous response is
                    # sent on this connection.
                    break
                except Exception:
                    break
                try:
                    await write_frame(
                        writer,
                        response,
                        max_frame_size=self._max_frame_size,
                    )
                except (ConnectionError, OSError):
                    break
        finally:
            if acquired:
                self._connection_limit.release()
            self._client_writers.discard(writer)
            if client_task is not None:
                self._client_tasks.discard(client_task)
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _authorize(self, request: Envelope) -> None:
        if request.kind is not MessageKind.REQUEST:
            raise WorkerProtocolError("worker server accepts requests only")
        self._authenticator.verify(request)
        if request.node_id != self.node_id:
            raise WorkerProtocolError("request targets another worker node")
        if request.session_epoch != self.session_epoch:
            raise WorkerProtocolError("request targets an expired worker session")

    async def _execute(self, request: Envelope) -> Envelope:
        try:
            async with self._inflight:
                result = await self._service.execute(request)
        except WorkerOperationError as error:
            return error_response(
                request,
                error,
                sequence=self._journal.next_sequence(),
                secret=self._secret,
            )
        except Exception:
            # Never put arbitrary driver exception details on the wire.
            return make_response(
                request,
                ok=False,
                error="worker operation failed",
                sequence=self._journal.next_sequence(),
                secret=self._secret,
            )
        return make_response(
            request,
            payload=result.payload,
            ok=result.ok,
            error=result.error,
            sequence=result.sequence,
            secret=self._secret,
        )

    @staticmethod
    def _validate_endpoint(
        host: str,
        allow_insecure_loopback: bool,
        ssl_context: ssl.SSLContext | None,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("worker host must be non-empty")
        if ssl_context is None and not allow_insecure_loopback:
            raise ValueError("TLS is required unless loopback plaintext is explicit")
        if ssl_context is None and host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("insecure worker transport is restricted to loopback")
        if ssl_context is not None and (
            ssl_context.protocol != ssl.PROTOCOL_TLS_SERVER
            or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2
        ):
            raise ValueError("worker server TLS requires a TLS 1.2+ server context")


__all__ = ["WorkerServer"]
