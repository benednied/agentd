"""Connection-reusing client for the authenticated worker protocol."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, TypeVar
from uuid import uuid4

from agentd.domain.models import (
    ExecutionContract,
    RunHandle,
    RunObservation,
    RunResult,
)
from agentd.harness.protocol import HarnessDriver
from agentd.workers.errors import (
    WorkerOperationError,
    WorkerProtocolError,
    WorkerStartUncertainError,
    WorkerTransportError,
)
from agentd.workers.protocol import validate_status_payload
from agentd.workers.remote_protocol import (
    MAX_FRAME_SIZE,
    Envelope,
    MessageKind,
    PSKAuthenticator,
    RemoteAction,
    make_request,
    operation_hash,
    read_frame,
    write_frame,
)

_ResponseT = TypeVar("_ResponseT")


class RemoteWorkerClient:
    """A bounded, reconnecting, request/response worker client.

    Retries preserve the request ID but use a fresh nonce.  The worker's
    operation journal therefore decides whether a side effect is replayed or
    merely acknowledged after an unknown network outcome.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        node_id: str,
        session_epoch: str,
        secret: bytes,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        allow_insecure_loopback: bool = False,
        max_frame_size: int = MAX_FRAME_SIZE,
        max_inflight: int = 16,
        connect_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._validate_endpoint(host, allow_insecure_loopback, ssl_context)
        if not 1 <= port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if max_frame_size <= 0 or max_frame_size > MAX_FRAME_SIZE:
            raise ValueError("max_frame_size is outside the protocol limit")
        if max_inflight <= 0:
            raise ValueError("max_inflight must be positive")
        if connect_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("connection and request timeouts must be positive")
        self.host = host
        self.port = port
        self.node_id = node_id
        self.session_epoch = session_epoch
        self._secret = bytes(secret)
        self._authenticator = PSKAuthenticator(secret)
        self._ssl_context = ssl_context
        self._server_hostname = server_hostname or host
        self._allow_insecure_loopback = allow_insecure_loopback
        self._max_frame_size = max_frame_size
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._inflight = asyncio.Semaphore(max_inflight)
        self._connection_lock = asyncio.Lock()
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._sequence = 0
        self._closed = False

    async def connect(self) -> None:
        async with self._connection_lock:
            await self._connect_locked()

    async def close(self) -> None:
        async with self._connection_lock:
            self._closed = True
            await self._close_locked()

    async def heartbeat(self, *, request_id: str | None = None) -> dict[str, Any]:
        return await self._call(
            RemoteAction.HEARTBEAT,
            {},
            run_id="",
            request_id=request_id,
            response_parser=self._heartbeat_from_dict,
        )

    async def start(
        self,
        driver: str | HarnessDriver,
        contract: ExecutionContract,
        *,
        run_id: str,
        managed: bool = False,
        request_id: str | None = None,
    ) -> RunHandle:
        driver_name = driver if isinstance(driver, str) else driver.capabilities().name
        payload = {
            "driver": driver_name,
            "contract": contract.to_dict(),
            "managed": managed,
        }
        return await self._call(
            RemoteAction.START,
            payload,
            run_id=run_id,
            request_id=request_id,
            response_parser=lambda value: self._start_from_dict(
                value,
                managed=managed,
            ),
        )

    async def status(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        result = await self._call(
            RemoteAction.STATUS,
            {},
            run_id=run_id,
            request_id=request_id,
            response_parser=validate_status_payload,
        )
        return result

    async def observe(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
    ) -> RunObservation | None:
        return await self._call(
            RemoteAction.OBSERVE,
            {},
            run_id=run_id,
            request_id=request_id,
            response_parser=self._observation_from_dict,
        )

    async def steer(
        self,
        run: RunHandle | str,
        instruction: str,
        *,
        request_id: str | None = None,
    ) -> None:
        await self._call(
            RemoteAction.STEER,
            {"instruction": instruction},
            run_id=self._run_id(run),
            request_id=request_id,
            response_parser=self._empty_response,
        )

    async def interrupt(
        self,
        run: RunHandle | str,
        *,
        request_id: str | None = None,
    ) -> None:
        await self._call(
            RemoteAction.INTERRUPT,
            {},
            run_id=self._run_id(run),
            request_id=request_id,
            response_parser=self._empty_response,
        )

    async def cancel(
        self,
        run: RunHandle | str,
        *,
        request_id: str | None = None,
    ) -> None:
        await self._call(
            RemoteAction.CANCEL,
            {},
            run_id=self._run_id(run),
            request_id=request_id,
            response_parser=self._empty_response,
        )

    async def collect(
        self,
        run: RunHandle | str,
        *,
        request_id: str | None = None,
    ) -> RunResult:
        return await self._call(
            RemoteAction.COLLECT,
            {},
            run_id=self._run_id(run),
            request_id=request_id,
            response_parser=self._result_from_dict,
        )

    async def _call(
        self,
        action: str | RemoteAction,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        request_id: str | None,
        response_parser: Callable[[dict[str, Any]], _ResponseT],
    ) -> _ResponseT:
        if self._closed:
            raise WorkerTransportError("worker client is closed")
        request_id = request_id or str(uuid4())
        async with self._inflight, self._connection_lock:
            for attempt in range(2):
                self._sequence += 1
                request = make_request(
                    action=action,
                    request_id=request_id,
                    node_id=self.node_id,
                    session_epoch=self.session_epoch,
                    run_id=run_id,
                    sequence=self._sequence,
                    payload=payload,
                    secret=self._secret,
                )
                try:
                    await self._connect_locked()
                    response = await self._roundtrip_locked(request)
                except asyncio.CancelledError:
                    # The request may already be on the wire and its response
                    # may arrive after caller cancellation. Closing prevents
                    # that stale frame from being consumed by the next call.
                    await self._close_locked()
                    raise
                except WorkerProtocolError as error:
                    # A framing/decode violation can originate inside
                    # ``read_frame`` before an Envelope exists. Never reuse
                    # that stream for another authenticated operation.
                    await self._close_locked()
                    if action == RemoteAction.START:
                        raise WorkerStartUncertainError(
                            "worker START outcome is unknown after protocol failure"
                        ) from error
                    raise
                except (ConnectionError, EOFError, OSError, TimeoutError) as error:
                    await self._close_locked()
                    if attempt == 0:
                        continue
                    if action == RemoteAction.START:
                        raise WorkerStartUncertainError(
                            "worker START outcome is unknown after transport failure"
                        ) from error
                    raise WorkerTransportError(
                        "worker transport failed after retry"
                    ) from error
                try:
                    if response.kind is not MessageKind.RESPONSE:
                        raise WorkerProtocolError("worker returned a non-response")
                    if (
                        response.request_id != request_id
                        or response.action != str(action)
                        or response.node_id != self.node_id
                        or response.session_epoch != self.session_epoch
                        or response.run_id != run_id
                        or response.request_hash
                        != operation_hash(action, run_id, payload)
                    ):
                        raise WorkerProtocolError("worker response identity mismatch")
                    self._authenticator.verify(response)
                except WorkerProtocolError as error:
                    # A malformed or unauthenticated response invalidates the
                    # reusable connection; never carry it into another call.
                    await self._close_locked()
                    if action == RemoteAction.START:
                        raise WorkerStartUncertainError(
                            "worker START outcome is unknown after protocol failure"
                        ) from error
                    raise
                if response.ok is not True:
                    # An authenticated negative START response may be a
                    # redacted worker-side failure after the driver touched an
                    # external system.  Ask STATUS using the same durable run
                    # id before the coordinator releases anything.
                    if action == RemoteAction.START:
                        raise WorkerStartUncertainError(
                            "worker START outcome is unknown after worker rejection"
                        ) from WorkerOperationError(
                            response.error or "worker operation failed"
                        )
                    raise WorkerOperationError(
                        response.error or "worker operation failed"
                    )
                try:
                    # Typed payload validation belongs to the authenticated
                    # roundtrip. If it ran after releasing this lock, another
                    # caller could reuse a stream whose previous response was
                    # malformed or could consume an orphaned response frame.
                    return response_parser(response.payload)
                except WorkerProtocolError as error:
                    await self._close_locked()
                    if action == RemoteAction.START:
                        raise WorkerStartUncertainError(
                            "worker START outcome is unknown after response "
                            "validation failure"
                        ) from error
                    raise
        raise WorkerTransportError("worker transport failed")  # pragma: no cover

    async def _connect_locked(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=self._ssl_context,
                    server_hostname=(
                        self._server_hostname if self._ssl_context is not None else None
                    ),
                    limit=self._max_frame_size + 4,
                ),
                timeout=self._connect_timeout,
            )
        except (ConnectionError, OSError, TimeoutError) as error:
            self._reader = None
            self._writer = None
            raise WorkerTransportError("could not connect to worker") from error

    async def _roundtrip_locked(self, request: Envelope) -> Envelope:
        reader, writer = self._reader, self._writer
        if reader is None or writer is None:
            raise WorkerTransportError("worker connection is not open")
        await write_frame(writer, request, max_frame_size=self._max_frame_size)
        return await asyncio.wait_for(
            read_frame(reader, max_frame_size=self._max_frame_size),
            timeout=self._request_timeout,
        )

    async def _close_locked(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    @staticmethod
    def _run_id(run: RunHandle | str) -> str:
        return run if isinstance(run, str) else run.id

    def _heartbeat_from_dict(self, value: dict[str, Any]) -> dict[str, Any]:
        required_fields = {
            "node_id",
            "session_epoch",
            "drivers",
            "driver_features",
            "active_runs",
        }
        drivers = value.get("drivers")
        driver_features = value.get("driver_features")
        active_runs = value.get("active_runs")
        if (
            set(value) != required_fields
            or value.get("node_id") != self.node_id
            or value.get("session_epoch") != self.session_epoch
            or not isinstance(drivers, list)
            or any(not isinstance(driver, str) for driver in drivers)
            or not isinstance(driver_features, dict)
            or isinstance(active_runs, bool)
            or not isinstance(active_runs, int)
            or active_runs < 0
        ):
            raise WorkerProtocolError("worker heartbeat is malformed")
        return value

    @classmethod
    def _start_from_dict(
        cls,
        value: dict[str, Any],
        *,
        managed: bool,
    ) -> RunHandle:
        if set(value) != {"handle", "managed"} or value.get("managed") is not managed:
            raise WorkerProtocolError("start response is malformed")
        handle_data = value.get("handle")
        if not isinstance(handle_data, dict):
            raise WorkerProtocolError("start response has no handle")
        return cls._handle_from_dict(handle_data)

    @staticmethod
    def _observation_from_dict(value: dict[str, Any]) -> RunObservation | None:
        if set(value) != {"observation"}:
            raise WorkerProtocolError("observe response is malformed")
        observation = value.get("observation")
        if observation is None:
            return None
        if not isinstance(observation, dict):
            raise WorkerProtocolError("observe response is malformed")
        try:
            return RunObservation.from_dict(observation)
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerProtocolError("observe response is malformed") from error

    @staticmethod
    def _result_from_dict(value: dict[str, Any]) -> RunResult:
        if set(value) != {"result"} or not isinstance(value.get("result"), dict):
            raise WorkerProtocolError("collect response is malformed")
        try:
            return RunResult.from_dict(value["result"])
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerProtocolError("collect response is malformed") from error

    @staticmethod
    def _empty_response(value: dict[str, Any]) -> None:
        if value:
            raise WorkerProtocolError("worker acknowledgement is malformed")

    @staticmethod
    def _handle_from_dict(data: dict[str, Any]) -> RunHandle:
        try:
            return RunHandle(
                id=data["id"],
                driver=data["driver"],
                external_id=data.get("external_id"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerProtocolError("worker handle is malformed") from error

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
            ssl_context.protocol != ssl.PROTOCOL_TLS_CLIENT
            or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2
            or ssl_context.verify_mode != ssl.CERT_REQUIRED
            or not ssl_context.check_hostname
        ):
            raise ValueError(
                "worker client TLS requires TLS 1.2+, certificate verification, "
                "and hostname checking"
            )


__all__ = ["RemoteWorkerClient"]
