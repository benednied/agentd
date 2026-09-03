from __future__ import annotations

import asyncio
import sqlite3
import ssl
from pathlib import Path

import pytest

from agentd.domain.enums import RunOutcome, RunState
from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunObservation,
    RunResult,
)
from agentd.harness.fake import FakeHarnessDriver
from agentd.workers import (
    OperationJournal,
    RemoteWorkerClient,
    WorkerProtocolError,
    WorkerServer,
    WorkerStartUncertainError,
    WorkerTransportError,
)
from agentd.workers.remote_protocol import make_request, make_response

SECRET = b"r" * 32


def _contract(working_directory: Path) -> ExecutionContract:
    return ExecutionContract(
        job_id="job-1",
        objective="exercise remote worker",
        scope="tests only",
        acceptance_criteria=("the typed lifecycle works",),
        dependency_results={},
        role="implementer",
        allowed_filesystem_scope=(str(working_directory),),
        checkpoint_expectations="checkpoint on request",
        coordination_mechanisms=("control-plane",),
        completion_protocol="return a run result",
        working_directory=str(working_directory),
        environment={},
        model_class="standard",
    )


class ManagedFakeDriver(FakeHarnessDriver):
    def __init__(self, *, id_factory) -> None:
        super().__init__(
            capabilities=HarnessCapabilities(
                name="managed-fake",
                models=frozenset({"standard"}),
                features=frozenset({"checkpointing", "steering"}),
                native_pause=True,
                steering=True,
                checkpointing=True,
            ),
            id_factory=id_factory,
        )
        self.start_calls = 0
        self.report_terminal = False

    async def start(self, execution: ExecutionContract):
        self.start_calls += 1
        return await super().start(execution)

    async def start_managed(self, run_id: str, execution: ExecutionContract):
        return await self.start(execution)

    async def recover(self, run_id: str, execution: ExecutionContract):
        return await self.start_managed(run_id, execution)

    def observe(self, run_id: str) -> RunObservation:
        return RunObservation(
            run_id=run_id,
            thread_id="thread-1",
            turn_id="turn-1",
            cursor="cursor-1",
            run_state=RunState.RUNNING,
        )

    def status(self, run: RunHandle) -> dict[str, object]:
        if not self.report_terminal:
            return {"known": True, "terminal": False, "result": None}
        return {
            "known": True,
            "terminal": True,
            "result": RunResult(
                RunOutcome.COMPLETED,
                "managed fake completed",
            ).to_dict(),
        }


class SlowFakeDriver(FakeHarnessDriver):
    def __init__(self, *, id_factory) -> None:
        super().__init__(id_factory=id_factory)
        self.start_calls = 0

    async def start(self, execution: ExecutionContract):
        self.start_calls += 1
        await asyncio.sleep(0.035)
        return await super().start(execution)


class HangingStartDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "hanging-handle")
        self.started = asyncio.Event()

    async def start(self, execution: ExecutionContract):
        self.started.set()
        await asyncio.Event().wait()
        return await super().start(execution)  # pragma: no cover


async def _open_server(
    tmp_path: Path,
    driver: FakeHarnessDriver,
) -> tuple[WorkerServer, RemoteWorkerClient]:
    journal = OperationJournal(
        tmp_path / "worker.sqlite3",
        node_id="node-1",
        session_epoch="epoch-1",
    )
    server = WorkerServer(
        "127.0.0.1",
        0,
        node_id="node-1",
        session_epoch="epoch-1",
        secret=SECRET,
        drivers=[driver],
        journal=journal,
        allow_insecure_loopback=True,
        request_timeout_seconds=2,
    )
    host, port = await server.start()
    client = RemoteWorkerClient(
        host,
        port,
        node_id="node-1",
        session_epoch="epoch-1",
        secret=SECRET,
        allow_insecure_loopback=True,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
    )
    return server, client


def test_authenticated_start_observe_collect_cancel_and_conflict(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ids = iter(("handle-1", "handle-2"))
        driver = ManagedFakeDriver(id_factory=lambda: next(ids))
        server, client = await _open_server(tmp_path, driver)
        contract = _contract(tmp_path)
        try:
            handle = await client.start(
                "managed-fake",
                contract,
                run_id="run-1",
                managed=True,
                request_id="start-1",
            )
            replayed = await client.start(
                "managed-fake",
                contract,
                run_id="run-1",
                managed=True,
                request_id="start-1",
            )
            assert replayed == handle
            assert driver.start_calls == 1
            assert await client.heartbeat(request_id="heartbeat-active") == {
                "node_id": "node-1",
                "session_epoch": "epoch-1",
                "drivers": ["managed-fake"],
                "driver_features": {"managed-fake": ["checkpointing", "steering"]},
                "active_runs": 1,
            }
            active_status = await client.status("run-1", request_id="status-active")
            assert active_status["known"] is True
            assert active_status["terminal"] is False

            observation = await client.observe("run-1", request_id="observe-1")
            assert observation is not None
            assert observation.run_id == "run-1"
            driver.report_terminal = True
            terminal_status = await client.status("run-1", request_id="status-terminal")
            assert terminal_status["known"] is True
            assert terminal_status["terminal"] is True
            assert terminal_status["result"]["outcome"] == RunOutcome.COMPLETED.value
            assert (await client.heartbeat(request_id="heartbeat-terminal"))[
                "active_runs"
            ] == 0

            result = await client.collect(handle, request_id="collect-1")
            assert result.outcome is RunOutcome.COMPLETED

            cancelled_handle = await client.start(
                "managed-fake",
                contract,
                run_id="run-2",
                managed=True,
                request_id="start-2",
            )
            await client.cancel(cancelled_handle, request_id="cancel-1")
            cancelled = await client.collect(
                cancelled_handle,
                request_id="collect-2",
            )
            assert cancelled.outcome is RunOutcome.CANCELLED

            altered = ExecutionContract(
                **{**contract.to_dict(), "objective": "different"}
            )
            with pytest.raises(WorkerStartUncertainError):
                await client.start(
                    "managed-fake",
                    altered,
                    run_id="run-1",
                    managed=True,
                    request_id="start-1",
                )
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_timeout_retries_same_request_id_without_duplicate_side_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = SlowFakeDriver(id_factory=lambda: "slow-handle")
        server, client = await _open_server(tmp_path, driver)
        client._request_timeout = 0.03
        contract = _contract(tmp_path)
        try:
            handle = await client.start(
                "fake",
                contract,
                run_id="run-1",
                request_id="slow-start",
            )
            assert handle.id == "slow-handle"
            assert driver.start_calls == 1
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_connection_reconnect_and_wrong_secret_are_closed_fail_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = FakeHarnessDriver()
        server, client = await _open_server(tmp_path, driver)
        try:
            first_heartbeat = await client.heartbeat(request_id="heartbeat-1")
            assert first_heartbeat == {
                "node_id": "node-1",
                "session_epoch": "epoch-1",
                "drivers": ["fake"],
                "driver_features": {"fake": ["checkpointing", "steering"]},
                "active_runs": 0,
            }
            assert client._writer is not None
            client._writer.close()
            await client._writer.wait_closed()
            second_heartbeat = await client.heartbeat(request_id="heartbeat-2")
            assert second_heartbeat == {
                "node_id": "node-1",
                "session_epoch": "epoch-1",
                "drivers": ["fake"],
                "driver_features": {"fake": ["checkpointing", "steering"]},
                "active_runs": 0,
            }
            for index in range(5):
                await client.heartbeat(request_id=f"heartbeat-fresh-{index}")
            unknown_status = await client.status(
                "run-after-restart",
                request_id="status-unknown",
            )
            assert unknown_status == {
                "known": False,
                "terminal": False,
                "result": None,
            }
            with sqlite3.connect(tmp_path / "worker.sqlite3") as connection:
                operation_count = connection.execute(
                    "SELECT COUNT(*) FROM worker_operations"
                ).fetchone()[0]
            assert operation_count == 0

            bad_client = RemoteWorkerClient(
                client.host,
                client.port,
                node_id="node-1",
                session_epoch="epoch-1",
                secret=b"b" * 32,
                allow_insecure_loopback=True,
                connect_timeout_seconds=1,
                request_timeout_seconds=1,
            )
            try:
                with pytest.raises(WorkerTransportError):
                    await bad_client.heartbeat(request_id="wrong-secret")
            finally:
                await bad_client.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_client_rejects_signed_response_for_different_request_payload() -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        original = make_request(
            action="steer",
            request_id="fixed-request",
            node_id="node-1",
            session_epoch="epoch-1",
            run_id="run-1",
            payload={"instruction": "old instruction"},
            secret=SECRET,
        )
        stale_response = make_response(original, secret=SECRET)
        closed = False

        async def fake_connect() -> None:
            return None

        async def fake_roundtrip(_request):
            return stale_response

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = fake_roundtrip
        client._close_locked = fake_close

        with pytest.raises(WorkerProtocolError, match="identity mismatch"):
            await client.steer(
                "run-1",
                "new instruction",
                request_id="fixed-request",
            )
        assert closed

    asyncio.run(scenario())


def test_client_closes_connection_after_response_protocol_error() -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        closed = False

        async def fake_connect() -> None:
            return None

        async def malformed_roundtrip(_request):
            raise WorkerProtocolError("malformed response")

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = malformed_roundtrip
        client._close_locked = fake_close

        with pytest.raises(WorkerProtocolError, match="malformed response"):
            await client.heartbeat(request_id="heartbeat-malformed")
        assert closed

    asyncio.run(scenario())


def test_client_closes_connection_after_typed_payload_error(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        closed = False

        async def fake_connect() -> None:
            return None

        async def malformed_roundtrip(request):
            return make_response(request, payload={}, secret=SECRET)

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = malformed_roundtrip
        client._close_locked = fake_close

        with pytest.raises(WorkerStartUncertainError, match="response validation"):
            await client.start(
                "managed-fake",
                _contract(tmp_path),
                run_id="run-malformed",
                request_id="start-malformed",
            )
        assert closed

    asyncio.run(scenario())


def test_client_treats_every_negative_start_response_as_uncertain(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        closed = False

        async def fake_connect() -> None:
            return None

        async def negative_roundtrip(request):
            return make_response(
                request,
                ok=False,
                error="worker operation failed",
                secret=SECRET,
            )

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = negative_roundtrip
        client._close_locked = fake_close

        with pytest.raises(WorkerStartUncertainError):
            await client.start(
                "managed-fake",
                _contract(tmp_path),
                run_id="run-negative",
                request_id="start-negative",
            )
        assert not closed

    asyncio.run(scenario())


def test_client_closes_connection_when_roundtrip_is_cancelled() -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        closed = False

        async def fake_connect() -> None:
            return None

        async def cancelled_roundtrip(_request):
            raise asyncio.CancelledError

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = cancelled_roundtrip
        client._close_locked = fake_close

        with pytest.raises(asyncio.CancelledError):
            await client.heartbeat(request_id="heartbeat-cancelled")
        assert closed

    asyncio.run(scenario())


def test_client_preserves_cancelled_start_as_uncertain() -> None:
    async def scenario() -> None:
        client = RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        closed = False

        async def fake_connect() -> None:
            return None

        async def cancelled_roundtrip(_request):
            raise asyncio.CancelledError

        async def fake_close() -> None:
            nonlocal closed
            closed = True

        client._connect_locked = fake_connect
        client._roundtrip_locked = cancelled_roundtrip
        client._close_locked = fake_close

        with pytest.raises(asyncio.CancelledError):
            await client.start(
                "fake",
                _contract(Path("/worker")),
                run_id="run-cancelled-start",
                request_id="start-cancelled",
            )
        assert closed

    asyncio.run(scenario())


def test_server_close_wakes_idle_authenticated_connections(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server, client = await _open_server(tmp_path, FakeHarnessDriver())
        host, port = server.address
        reader, writer = await asyncio.open_connection(host, port)
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                if server._client_writers:
                    break
            assert server._client_writers
            await asyncio.wait_for(server.close(), timeout=0.5)
            assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_server_times_out_idle_peer_and_releases_connection_slot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        journal = OperationJournal(
            tmp_path / "idle-timeout-worker.sqlite3",
            node_id="node-1",
            session_epoch="epoch-1",
        )
        server = WorkerServer(
            "127.0.0.1",
            0,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            drivers=[FakeHarnessDriver()],
            journal=journal,
            allow_insecure_loopback=True,
            max_connections=1,
            frame_timeout_seconds=0.05,
        )
        host, port = await server.start()
        idle_reader, idle_writer = await asyncio.open_connection(host, port)
        client = RemoteWorkerClient(
            host,
            port,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        try:
            assert await asyncio.wait_for(idle_reader.read(), timeout=0.5) == b""
            snapshot = await asyncio.wait_for(client.heartbeat(), timeout=0.5)
            assert snapshot["node_id"] == "node-1"
        finally:
            idle_writer.close()
            await idle_writer.wait_closed()
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_server_close_cancels_hanging_operations_before_journal_close(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = HangingStartDriver()
        server, client = await _open_server(tmp_path, driver)
        server._shutdown_timeout = 0.2
        contract = _contract(tmp_path)
        operation = asyncio.create_task(
            client.start(
                "fake",
                contract,
                run_id="hanging-run",
                request_id="hanging-start",
            )
        )
        try:
            await asyncio.wait_for(driver.started.wait(), timeout=0.5)
            await asyncio.wait_for(server.close(), timeout=0.5)
            with pytest.raises(WorkerTransportError):
                await operation
            assert not server._background_tasks
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            await client.close()
            await server.close()

    asyncio.run(scenario())


def test_remote_defaults_require_tls_and_allow_plaintext_only_on_loopback(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="TLS is required"):
        WorkerServer(
            "127.0.0.1",
            0,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
        )
    with pytest.raises(ValueError, match="TLS is required"):
        RemoteWorkerClient(
            "127.0.0.1",
            1,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
        )
    with pytest.raises(ValueError, match="restricted to loopback"):
        WorkerServer(
            "worker.example",
            0,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )

    with pytest.raises(ValueError, match="durable operation journal"):
        WorkerServer(
            "127.0.0.1",
            0,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
    memory_journal = OperationJournal(
        ":memory:",
        node_id="node-1",
        session_epoch="epoch-1",
    )
    try:
        with pytest.raises(ValueError, match="file-backed"):
            WorkerServer(
                "127.0.0.1",
                0,
                node_id="node-1",
                session_epoch="epoch-1",
                secret=SECRET,
                journal=memory_journal,
                allow_insecure_loopback=True,
            )
    finally:
        memory_journal.close()

    mismatched_journal = OperationJournal(
        tmp_path / "mismatched-worker.sqlite3",
        node_id="another-node",
        session_epoch="another-epoch",
    )
    try:
        with pytest.raises(ValueError, match="journal identity"):
            WorkerServer(
                "127.0.0.1",
                0,
                node_id="node-1",
                session_epoch="epoch-1",
                secret=SECRET,
                journal=mismatched_journal,
                allow_insecure_loopback=True,
            )
    finally:
        mismatched_journal.close()

    durable_journal = OperationJournal(
        tmp_path / "durable-worker.sqlite3",
        node_id="node-1",
        session_epoch="epoch-1",
    )
    durable_server = WorkerServer(
        "127.0.0.1",
        0,
        node_id="node-1",
        session_epoch="epoch-1",
        secret=SECRET,
        journal=durable_journal,
        allow_insecure_loopback=True,
        tls_handshake_timeout_seconds=3,
    )
    assert durable_server._tls_handshake_timeout == 3
    asyncio.run(durable_server.close())

    weak_server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    weak_server_tls.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    with pytest.raises(ValueError, match=r"TLS 1\.2"):
        WorkerServer(
            "worker.example",
            443,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            ssl_context=weak_server_tls,
        )

    weak_client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    weak_client_tls.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    with pytest.raises(ValueError, match=r"TLS 1\.2"):
        RemoteWorkerClient(
            "worker.example",
            443,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            ssl_context=weak_client_tls,
        )

    unverified_client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified_client_tls.minimum_version = ssl.TLSVersion.TLSv1_2
    unverified_client_tls.check_hostname = False
    unverified_client_tls.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="certificate verification"):
        RemoteWorkerClient(
            "worker.example",
            443,
            node_id="node-1",
            session_epoch="epoch-1",
            secret=SECRET,
            ssl_context=unverified_client_tls,
        )
