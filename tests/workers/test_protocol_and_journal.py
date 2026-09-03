from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.domain.enums import RunOutcome
from agentd.domain.models import ExecutionContract, RunResult
from agentd.harness.fake import FakeHarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.workers.errors import (
    WorkerAuthenticationError,
    WorkerJournalConflictError,
    WorkerProtocolError,
)
from agentd.workers.execution import ExecutionService
from agentd.workers.journal import OperationJournal
from agentd.workers.protocol import validate_status_payload
from agentd.workers.remote_protocol import (
    MAX_FRAME_SIZE,
    MAX_STRING_SIZE,
    PSKAuthenticator,
    canonical_json,
    decode_envelope,
    encode_envelope,
    envelope_from_dict,
    error_response,
    make_request,
    payload_hash,
)

SECRET = b"s" * 32


def _execution_contract() -> ExecutionContract:
    return ExecutionContract(
        job_id="job-1",
        objective="worker test",
        scope="tests",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=(),
        checkpoint_expectations="none",
        coordination_mechanisms=(),
        completion_protocol="return a result",
        working_directory="",
        environment={},
        model_class="standard",
    )


def _operation_request(
    action: str,
    request_id: str,
    run_id: str,
    *,
    payload: dict | None = None,
):
    return make_request(
        action=action,
        request_id=request_id,
        node_id="node-1",
        session_epoch="epoch-1",
        run_id=run_id,
        payload=payload,
        secret=SECRET,
    )


def _start_request(request_id: str, run_id: str, *, objective: str = "worker test"):
    contract = ExecutionContract(
        **{**_execution_contract().to_dict(), "objective": objective}
    )
    return _operation_request(
        "start",
        request_id,
        run_id,
        payload={
            "driver": "fake",
            "contract": contract.to_dict(),
            "managed": False,
        },
    )


class BlockingStartDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "blocking-handle")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.start_calls = 0

    async def start(self, execution: ExecutionContract):
        self.start_calls += 1
        self.started.set()
        await self.release.wait()
        return await super().start(execution)


class CountingStartDriver(FakeHarnessDriver):
    def __init__(self, handle_id: str) -> None:
        super().__init__(id_factory=lambda: handle_id)
        self.start_calls = 0

    async def start(self, execution: ExecutionContract):
        self.start_calls += 1
        return await super().start(execution)


class FailingStartDriver(FakeHarnessDriver):
    async def start(self, execution: ExecutionContract):
        del execution
        raise RuntimeError("start rejected before a run was created")


async def _start_operation(
    service: ExecutionService,
    run_id: str,
    request_id: str,
) -> None:
    response = await service.execute(
        _operation_request(
            "start",
            request_id,
            run_id,
            payload={
                "driver": "fake",
                "contract": _execution_contract().to_dict(),
                "managed": False,
            },
        )
    )
    assert response.ok


def test_protocol_is_versioned_bounded_and_fail_closed() -> None:
    request = make_request(
        action="heartbeat",
        request_id="request-1",
        node_id="node-1",
        session_epoch="epoch-1",
        secret=SECRET,
        timestamp=100.0,
        nonce="n" * 32,
    )
    raw = request.to_dict()

    with pytest.raises(WorkerProtocolError, match="unsupported protocol version"):
        envelope_from_dict({**raw, "version": 2})
    with pytest.raises(WorkerProtocolError, match="version must be an integer"):
        envelope_from_dict({**raw, "version": True})
    with pytest.raises(WorkerProtocolError, match="timestamp must be finite"):
        envelope_from_dict({**raw, "timestamp": 10**1_000})
    with pytest.raises(WorkerProtocolError, match="valid UTF-8 JSON"):
        decode_envelope(b"not-json")
    with pytest.raises(WorkerProtocolError, match="valid UTF-8 JSON"):
        decode_envelope(b'{"value":NaN}')
    with pytest.raises(WorkerProtocolError, match="valid UTF-8 JSON"):
        decode_envelope(b'{"kind":"request","kind":"request"}')
    with pytest.raises(WorkerProtocolError, match="valid UTF-8 JSON"):
        decode_envelope(b"[" * 10_000 + b"0" + b"]" * 10_000)

    oversized = make_request(
        action="heartbeat",
        request_id="request-2",
        node_id="node-1",
        session_epoch="epoch-1",
        payload={"items": ["x" * MAX_STRING_SIZE for _ in range(64)]},
        secret=SECRET,
    )
    with pytest.raises(WorkerProtocolError, match="frame limit"):
        encode_envelope(oversized)
    assert MAX_FRAME_SIZE > MAX_STRING_SIZE


def test_authentication_is_constant_time_and_replay_protected() -> None:
    now = [100.0]
    authenticator = PSKAuthenticator(SECRET, clock=lambda: now[0])
    request = make_request(
        action="heartbeat",
        request_id="request-1",
        node_id="node-1",
        session_epoch="epoch-1",
        secret=SECRET,
        timestamp=100.0,
        nonce="n" * 32,
    )
    authenticator.verify(request)
    with pytest.raises(WorkerAuthenticationError, match="already used"):
        authenticator.verify(request)

    with pytest.raises(ValueError, match="32 bytes"):
        PSKAuthenticator(b"too-short")
    tampered = request.to_dict()
    tampered["auth"] = "0" * 64
    with pytest.raises(WorkerAuthenticationError) as error:
        authenticator.verify(envelope_from_dict(tampered))
    assert SECRET.decode() not in str(error.value)

    safe = error_response(
        request,
        RuntimeError("secret=" + SECRET.decode()),
        sequence=1,
        secret=SECRET,
    )
    assert SECRET.decode() not in canonical_json(safe.to_dict()).decode()


def test_status_result_is_a_fully_validated_run_result() -> None:
    result = RunResult(RunOutcome.COMPLETED, "done").to_dict()
    validated = validate_status_payload(
        {"known": True, "terminal": True, "result": result}
    )
    assert validated["result"] == result

    with pytest.raises(WorkerProtocolError, match="result is malformed"):
        validate_status_payload(
            {
                "known": True,
                "terminal": True,
                "result": {"outcome": "not-a-run-outcome"},
            }
        )


def test_heartbeat_reports_features_of_registered_drivers(tmp_path: Path) -> None:
    journal = OperationJournal(
        tmp_path / "worker.sqlite3",
        node_id="node-1",
        session_epoch="epoch-1",
    )
    service = ExecutionService(DriverRegistry((FakeHarnessDriver(),)), journal)
    try:
        response = asyncio.run(
            service.execute(_operation_request("heartbeat", "heartbeat-1", ""))
        )
        assert response.ok is True
        assert response.payload["drivers"] == ["fake"]
        assert response.payload["driver_features"] == {
            "fake": ["checkpointing", "steering"]
        }
    finally:
        journal.close()


def test_operation_journal_replays_conflicts_and_sequences_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.sqlite3"
    digest = payload_hash({"value": "one"})
    with OperationJournal(path, node_id="node-1", session_epoch="epoch-1") as journal:
        pending = journal.begin(
            request_id="request-1",
            action="start",
            payload_hash=digest,
        )
        assert pending.reserved_here
        assert (
            journal.begin(
                request_id="request-1",
                action="start",
                payload_hash=digest,
            ).reserved_here
            is False
        )

        with pytest.raises(WorkerJournalConflictError):
            journal.begin(
                request_id="request-1",
                action="start",
                payload_hash=payload_hash({"value": "two"}),
            )

        response = {"ok": True, "payload": {"handle": {"id": "h-1"}}}
        completed = journal.complete(
            request_id="request-1",
            action="start",
            payload_hash=digest,
            response=response,
            sequence=journal.next_sequence(),
        )
        assert completed.status == "completed"
        first_sequence = completed.sequence
        assert first_sequence is not None
        assert path.stat().st_mode & 0o777 == 0o600
        journal.begin(
            request_id="pending-after-crash",
            action="start",
            payload_hash=payload_hash({"pending": True}),
        )

    with OperationJournal(path, node_id="node-1", session_epoch="epoch-1") as restarted:
        replay = restarted.lookup(
            request_id="request-1",
            action="start",
            payload_hash=digest,
        )
        assert replay is not None
        assert replay.response == response
        assert replay.sequence == first_sequence
        assert restarted.next_sequence() > first_sequence
        pending = restarted.begin(
            request_id="pending-after-crash",
            action="start",
            payload_hash=payload_hash({"pending": True}),
        )
        assert pending.status == "pending"
        assert pending.reserved_here is False


def test_operation_journal_claims_run_ids_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    first_hash = payload_hash({"contract": "one"})
    second_hash = payload_hash({"contract": "two"})

    with OperationJournal(path, node_id="node-1", session_epoch="epoch-1") as journal:
        assert journal.claim_run(run_id="run-1", start_hash=first_hash)
        assert journal.run_claim_state(run_id="run-1") == "claimed"
        journal.mark_run_started(run_id="run-1", start_hash=first_hash)
        assert journal.run_claim_state(run_id="run-1") == "started"
        assert not journal.claim_run(run_id="run-1", start_hash=first_hash)

    with OperationJournal(path, node_id="node-1", session_epoch="epoch-1") as journal:
        assert not journal.claim_run(run_id="run-1", start_hash=first_hash)
        with pytest.raises(WorkerJournalConflictError, match="different start"):
            journal.claim_run(run_id="run-1", start_hash=second_hash)

    # A deliberate new worker session is a separate idempotency namespace.
    with OperationJournal(path, node_id="node-1", session_epoch="epoch-2") as journal:
        assert journal.claim_run(run_id="run-1", start_hash=second_hash)


def test_execution_service_status_preserves_durable_claim_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.sqlite3"
    start_hash = payload_hash(_start_request("start-a", "run-1").payload)
    with OperationJournal(path, node_id="node-1", session_epoch="epoch-1") as journal:
        assert journal.claim_run(run_id="run-1", start_hash=start_hash)

    async def scenario() -> None:
        with OperationJournal(
            path, node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(DriverRegistry((FakeHarnessDriver(),)), journal)
            status = await service.execute(
                _operation_request("status", "status-after-restart", "run-1")
            )
            assert status.payload == {
                "known": True,
                "terminal": False,
                "result": None,
            }

    asyncio.run(scenario())


def test_failed_start_claim_stays_unresolved_to_prevent_duplicate() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(
                DriverRegistry((FailingStartDriver(),)),
                journal,
            )
            response = await service.execute(_start_request("start-fails", "run-fails"))
            assert not response.ok
            assert journal.run_claim_state(run_id="run-fails") == "claimed"
            status = await service.execute(
                _operation_request("status", "status-failed", "run-fails")
            )
            assert status.payload == {
                "known": True,
                "terminal": False,
                "result": None,
            }

    asyncio.run(scenario())


def test_execution_service_does_not_claim_safe_managed_start_rejection() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(DriverRegistry((FakeHarnessDriver(),)), journal)
            request = _start_request("start-invalid-managed", "run-invalid")
            request = make_request(
                action="start",
                request_id=request.request_id,
                node_id=request.node_id,
                session_epoch=request.session_epoch,
                run_id=request.run_id,
                payload={**request.payload, "managed": True},
                secret=SECRET,
            )
            response = await service.execute(request)
            assert not response.ok
            assert journal.run_claim_state(run_id="run-invalid") is None

    asyncio.run(scenario())


def test_execution_service_rejects_start_request_reuse_across_runs() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(
                DriverRegistry((FakeHarnessDriver(),)),
                journal,
            )
            await _start_operation(service, "run-1", "shared-start")
            with pytest.raises(WorkerJournalConflictError, match="different payload"):
                await _start_operation(service, "run-2", "shared-start")

    asyncio.run(scenario())


def test_execution_service_gates_concurrent_starts_by_durable_run_id() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            driver = BlockingStartDriver()
            service = ExecutionService(DriverRegistry((driver,)), journal)
            first_task = asyncio.create_task(
                service.execute(_start_request("start-a", "run-1"))
            )
            await driver.started.wait()
            second_task = asyncio.create_task(
                service.execute(_start_request("start-b", "run-1"))
            )
            await asyncio.sleep(0)
            assert not second_task.done()
            driver.release.set()
            first, second = await asyncio.gather(first_task, second_task)

            assert first.ok and second.ok
            assert first.payload == second.payload
            assert driver.start_calls == 1
            assert service._runs["run-1"].handle.id == "blocking-handle"
            assert len(service._runs) == 2  # durable id plus harness-id alias

    asyncio.run(scenario())


def test_status_during_start_never_reports_authoritative_unknown() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            driver = BlockingStartDriver()
            service = ExecutionService(DriverRegistry((driver,)), journal)
            start_task = asyncio.create_task(
                service.execute(_start_request("start-a", "run-1"))
            )
            await driver.started.wait()
            status_task = asyncio.create_task(
                service.execute(_operation_request("status", "status-a", "run-1"))
            )
            await asyncio.sleep(0)
            assert not status_task.done()
            driver.release.set()
            status = await status_task
            assert status.payload == {
                "known": True,
                "terminal": False,
                "result": None,
            }
            started = await start_task
            assert started.ok

    asyncio.run(scenario())


def test_execution_service_rejects_conflicting_concurrent_start_without_overwrite() -> (
    None
):
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            driver = BlockingStartDriver()
            service = ExecutionService(DriverRegistry((driver,)), journal)
            first_task = asyncio.create_task(
                service.execute(_start_request("start-a", "run-1"))
            )
            await driver.started.wait()
            conflict_task = asyncio.create_task(
                service.execute(
                    _start_request(
                        "start-b",
                        "run-1",
                        objective="different worker contract",
                    )
                )
            )
            await asyncio.sleep(0)
            assert not conflict_task.done()
            driver.release.set()
            first, conflict = await asyncio.gather(first_task, conflict_task)

            assert first.ok
            assert not conflict.ok
            assert conflict.error is not None
            assert driver.start_calls == 1
            assert service._runs["run-1"].start_fingerprint == payload_hash(
                _start_request("start-a", "run-1").payload
            )

    asyncio.run(scenario())


def test_execution_service_never_restarts_claimed_run_after_worker_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "worker.sqlite3"
        first_driver = CountingStartDriver("first-handle")
        with OperationJournal(
            path, node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            first_service = ExecutionService(
                DriverRegistry((first_driver,)),
                journal,
            )
            first = await first_service.execute(_start_request("start-a", "run-1"))
            assert first.ok
            assert first_driver.start_calls == 1

        restarted_driver = CountingStartDriver("duplicate-handle")
        with OperationJournal(
            path, node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            restarted_service = ExecutionService(
                DriverRegistry((restarted_driver,)),
                journal,
            )
            repeated = await restarted_service.execute(
                _start_request("start-b", "run-1")
            )
            conflicting = await restarted_service.execute(
                _start_request(
                    "start-c",
                    "run-1",
                    objective="different worker contract",
                )
            )

            assert not repeated.ok
            assert not conflicting.ok
            assert restarted_driver.start_calls == 0
            assert restarted_service._runs == {}

    asyncio.run(scenario())


def test_execution_service_rejects_cancel_request_reuse_across_runs() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(
                DriverRegistry((FakeHarnessDriver(),)),
                journal,
            )
            await _start_operation(service, "run-1", "start-1")
            await _start_operation(service, "run-2", "start-2")
            first = await service.execute(
                _operation_request("cancel", "shared-cancel", "run-1")
            )
            assert first.ok
            with pytest.raises(WorkerJournalConflictError, match="different payload"):
                await service.execute(
                    _operation_request("cancel", "shared-cancel", "run-2")
                )

    asyncio.run(scenario())


def test_execution_service_rejects_collect_request_reuse_across_runs() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            service = ExecutionService(
                DriverRegistry((FakeHarnessDriver(),)),
                journal,
            )
            await _start_operation(service, "run-1", "start-1")
            await _start_operation(service, "run-2", "start-2")
            first = await service.execute(
                _operation_request("collect", "shared-collect", "run-1")
            )
            assert first.ok
            with pytest.raises(WorkerJournalConflictError, match="different payload"):
                await service.execute(
                    _operation_request("collect", "shared-collect", "run-2")
                )

    asyncio.run(scenario())


def test_execution_service_releases_request_locks_without_racing_duplicates() -> None:
    async def scenario() -> None:
        with OperationJournal(
            ":memory:", node_id="node-1", session_epoch="epoch-1"
        ) as journal:
            driver = FakeHarnessDriver(id_factory=lambda: "handle-1")
            service = ExecutionService(DriverRegistry((driver,)), journal)

            start_request = _operation_request(
                "start",
                "duplicate-start",
                "run-1",
                payload={
                    "driver": "fake",
                    "contract": _execution_contract().to_dict(),
                    "managed": False,
                },
            )
            first, second = await asyncio.gather(
                service.execute(start_request),
                service.execute(start_request),
            )
            assert first == second
            assert len(service._request_locks) == 0

            # Invalid/unknown operations still exercise the same lifecycle;
            # each completed call must release its lock entry instead of
            # growing the map for the lifetime of the worker process.
            for index in range(32):
                response = await service.execute(
                    _operation_request(
                        "observe",
                        f"unknown-observe-{index}",
                        f"missing-run-{index}",
                    )
                )
                assert not response.ok
            assert len(service._request_locks) == 0

    asyncio.run(scenario())
