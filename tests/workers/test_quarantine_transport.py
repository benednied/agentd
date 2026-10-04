"""Physical retirement releases capacity without making metering claims."""

from __future__ import annotations

import asyncio

import pytest

from agentd.domain.models import ExecutionContract
from agentd.harness.fake import FakeHarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.workers.client import RemoteWorkerClient
from agentd.workers.errors import (
    WorkerJournalConflictError,
    WorkerOperationError,
    WorkerProtocolError,
    WorkerStartUncertainError,
)
from agentd.workers.execution import ExecutionService
from agentd.workers.journal import OperationJournal
from agentd.workers.remote import RemoteWorkerBackend
from agentd.workers.remote_protocol import (
    make_request,
    payload_hash,
    validate_retirement_certificate,
)
from agentd.workers.server import WorkerServer

SECRET = b"q" * 32


def contract():
    return ExecutionContract(
        job_id="job",
        objective="controlled quarantine test",
        scope="tests",
        acceptance_criteria=(),
        dependency_results={},
        role="implementer",
        allowed_filesystem_scope=(),
        checkpoint_expectations="none",
        coordination_mechanisms=(),
        completion_protocol="return result",
        working_directory="",
        environment={},
        model_class="standard",
    )


def start_payload():
    return {"driver": "fake", "contract": contract().to_dict(), "managed": False}


def stop_proof(**changes):
    return {
        "proof_version": 1,
        "job_id": "job",
        "node_id": "node",
        "session_epoch": "epoch",
        "run_id": "ghost",
        "start_hash": payload_hash(start_payload()),
        "actor": "github:owner:7",
        "event_id": "github:42:comment:123",
        "container_id": "c" * 64,
        "stopped_at": "2026-10-03T11:59:00Z",
        "running": False,
        "pid": 0,
        **changes,
    }


def retirement(proof):
    return {
        key: value for key, value in proof.items() if key not in {"running", "pid"}
    } | {
        "job_id": "job",
        "physical_retired": True,
        "metering_unknown": True,
        "stop_proof_sha256": payload_hash(proof),
        "fenced_at": "2026-10-03T12:00:00Z",
    }


def request(action, run_id="ghost", *, request_id=None, payload=None):
    return make_request(
        action=action,
        run_id=run_id,
        request_id=request_id or action,
        node_id="node",
        session_epoch="epoch",
        payload=payload,
        secret=SECRET,
    )


def quarantine_payload(proof):
    return {key: proof[key] for key in ("actor", "event_id", "start_hash")} | {
        "stop_proof": proof,
    }


class FencedDriver(FakeHarnessDriver):
    def __init__(self):
        super().__init__(id_factory=lambda: "fresh-handle")
        self.quarantine_calls = 0
        self.start_calls = 0
        self.proof = stop_proof()

    def load_terminal_result(self, _run_id):
        return None

    async def recover_terminal(self, _run_id):
        raise WorkerOperationError("provider ownership is unknown")

    async def quarantine_run(self, run_id, *, actor, event_id, stop_proof, start_hash):
        self.quarantine_calls += 1
        if (
            stop_proof != self.proof
            or run_id != self.proof["run_id"]
            or actor != self.proof["actor"]
            or event_id != self.proof["event_id"]
            or start_hash != self.proof["start_hash"]
        ):
            raise WorkerOperationError("protected stop proof mismatch")
        return retirement(stop_proof)

    async def start(self, execution):
        self.start_calls += 1
        return await super().start(execution)


def test_unknown_claim_can_be_retired_without_result_then_fresh_run_starts(tmp_path):
    async def scenario():
        path = tmp_path / "journal.sqlite"
        proof = stop_proof()
        with OperationJournal(path, node_id="node", session_epoch="epoch") as journal:
            journal.claim_run(run_id="ghost", start_hash=proof["start_hash"])
            driver = FencedDriver()
            service = ExecutionService(DriverRegistry([driver]), journal)
            assert (await service.execute(request("heartbeat", ""))).payload[
                "active_runs"
            ] == 1
            status = await service.execute(request("status"))
            assert status.payload == {"known": True, "terminal": False, "result": None}
            rejected = await service.execute(
                request(
                    "start", "fresh", request_id="before-fence", payload=start_payload()
                )
            )
            assert not rejected.ok and journal.run_claim_state(run_id="fresh") is None
            retired = await service.execute(
                request("quarantine-run", payload=quarantine_payload(proof))
            )
            assert retired.ok and retired.payload == {"retirement": retirement(proof)}
            assert journal.run_claim_state(run_id="ghost") == "claimed"
            assert journal.load_run_result(run_id="ghost") is None
            assert journal.unresolved_run_ids() == ()
            assert (await service.execute(request("heartbeat", ""))).payload[
                "active_runs"
            ] == 0
            fresh = await service.execute(
                request(
                    "start", "fresh", request_id="after-fence", payload=start_payload()
                )
            )
            assert fresh.ok and driver.start_calls == 1
        with OperationJournal(path, node_id="node", session_epoch="epoch") as journal:
            assert journal.load_run_retirement(run_id="ghost") == retirement(proof)
            assert journal.run_claim_hash(run_id="ghost") == proof["start_hash"]
            with pytest.raises(WorkerJournalConflictError):
                journal.claim_run(run_id="ghost", start_hash=proof["start_hash"])
            journal.retire_run(
                run_id="ghost",
                start_hash=proof["start_hash"],
                certificate=retirement(proof),
            )
            with pytest.raises(WorkerJournalConflictError, match="changed"):
                journal.retire_run(
                    run_id="ghost",
                    start_hash=proof["start_hash"],
                    certificate=retirement(stop_proof(event_id="different")),
                )
        with (
            OperationJournal(
                path, node_id="node", session_epoch="new-epoch"
            ) as journal,
            pytest.raises(WorkerJournalConflictError),
        ):
            journal.claim_run(run_id="ghost", start_hash=proof["start_hash"])

    asyncio.run(scenario())


def test_retirement_blocks_replay_of_prior_successful_start_response(tmp_path):
    async def scenario():
        with OperationJournal(
            tmp_path / "journal.sqlite", node_id="node", session_epoch="epoch"
        ) as journal:
            original = request("start", payload=start_payload())
            digest = payload_hash(
                {"run_id": original.run_id, "payload": original.payload}
            )
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
            journal.mark_run_started(
                run_id="ghost", start_hash=stop_proof()["start_hash"]
            )
            journal.begin(request_id="start", action="start", payload_hash=digest)
            journal.complete(
                request_id="start",
                action="start",
                payload_hash=digest,
                response={
                    "ok": True,
                    "payload": {
                        "handle": {"id": "old", "driver": "fake"},
                        "managed": False,
                    },
                    "error": None,
                },
                sequence=journal.next_sequence(),
            )
            driver = FencedDriver()
            service = ExecutionService(DriverRegistry([driver]), journal)
            q = request("quarantine-run", payload=quarantine_payload(stop_proof()))
            assert (await service.execute(q)).ok
            assert (await service.execute(q)).ok and driver.quarantine_calls == 1
            with pytest.raises(WorkerOperationError, match="retired"):
                await service.execute(original)
            with pytest.raises(WorkerOperationError, match="retired"):
                await service.execute(
                    request("start", request_id="another", payload=start_payload())
                )
            assert driver.start_calls == 0

    asyncio.run(scenario())


def test_retirement_does_not_remove_other_claim_or_its_capacity_hold(tmp_path):
    async def scenario():
        with OperationJournal(
            tmp_path / "journal.sqlite", node_id="node", session_epoch="epoch"
        ) as journal:
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
            journal.claim_run(run_id="other-ghost", start_hash="d" * 64)
            service = ExecutionService(DriverRegistry([FencedDriver()]), journal)
            assert (
                await service.execute(
                    request("quarantine-run", payload=quarantine_payload(stop_proof()))
                )
            ).ok
            assert journal.unresolved_run_ids() == ("other-ghost",)
            assert (await service.execute(request("heartbeat", ""))).payload[
                "active_runs"
            ] == 1
            assert not (
                await service.execute(
                    request("start", "fresh", payload=start_payload())
                )
            ).ok
            assert journal.run_claim_state(run_id="fresh") is None

    asyncio.run(scenario())


def test_exact_pending_quarantine_reconciles_after_restart_without_repeating_driver(
    tmp_path,
):
    async def scenario():
        proof = stop_proof()
        q = request("quarantine-run", payload=quarantine_payload(proof))
        path = tmp_path / "journal.sqlite"
        with OperationJournal(path, node_id="node", session_epoch="epoch") as journal:
            journal.claim_run(run_id="ghost", start_hash=proof["start_hash"])
            journal.begin(
                request_id=q.request_id,
                action=q.action,
                payload_hash=payload_hash({"run_id": q.run_id, "payload": q.payload}),
            )
            journal.retire_run(
                run_id="ghost",
                start_hash=proof["start_hash"],
                certificate=retirement(proof),
            )
        with OperationJournal(path, node_id="node", session_epoch="epoch") as journal:
            driver = FencedDriver()
            service = ExecutionService(DriverRegistry([driver]), journal)
            result = await service.execute(q)
            assert result.ok and result.payload["retirement"] == retirement(proof)
            assert driver.quarantine_calls == 0
            assert (await service.execute(q)) == result
            assert journal.load_run_result(run_id="ghost") is None
            with pytest.raises(WorkerJournalConflictError, match="cannot change"):
                journal.mark_run_started(run_id="ghost", start_hash=proof["start_hash"])

    asyncio.run(scenario())


def test_quarantine_rejects_local_run_and_retains_unknown_claim(tmp_path):
    async def scenario():
        with OperationJournal(
            tmp_path / "journal.sqlite", node_id="node", session_epoch="epoch"
        ) as journal:
            driver = FencedDriver()
            service = ExecutionService(DriverRegistry([driver]), journal)
            assert (
                await service.execute(request("start", "live", payload=start_payload()))
            ).ok
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
            rejected = await service.execute(
                request("quarantine-run", payload=quarantine_payload(stop_proof()))
            )
            assert not rejected.ok and driver.quarantine_calls == 0
            assert journal.load_run_retirement(run_id="ghost") is None

    asyncio.run(scenario())


def test_quarantine_is_serialized_with_pending_start(tmp_path):
    async def scenario():
        with OperationJournal(
            tmp_path / "journal.sqlite", node_id="node", session_epoch="epoch"
        ) as journal:
            driver = FencedDriver()
            service = ExecutionService(DriverRegistry([driver]), journal)
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
            await service._start_lock.acquire()
            starting = asyncio.create_task(
                service.execute(request("start", "ghost", payload=start_payload()))
            )
            await asyncio.sleep(0)
            retiring = asyncio.create_task(
                service.execute(
                    request("quarantine-run", payload=quarantine_payload(stop_proof()))
                )
            )
            await asyncio.sleep(0)
            assert not retiring.done() and driver.quarantine_calls == 0
            service._start_lock.release()
            assert not (await starting).ok
            assert (await retiring).ok
            assert driver.start_calls == 0 and driver.quarantine_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"start_hash": "d" * 64},
        {"actor": "github:other:8"},
        {"event_id": "different"},
        {"container_id": "e" * 64},
        {"pid": 1},
        {"running": True},
    ],
)
def test_quarantine_rejects_unbound_stop_proof_and_preserves_capacity(
    tmp_path, changes
):
    async def scenario():
        with OperationJournal(
            tmp_path / "journal.sqlite", node_id="node", session_epoch="epoch"
        ) as journal:
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
            service = ExecutionService(DriverRegistry([FencedDriver()]), journal)
            response = await service.execute(
                request(
                    "quarantine-run", payload=quarantine_payload(stop_proof(**changes))
                )
            )
            assert not response.ok
            assert journal.load_run_retirement(run_id="ghost") is None
            assert journal.unresolved_run_ids() == ("ghost",)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"physical_retired": False},
        {"metering_unknown": False},
        {"start_hash": "d" * 64},
        {"stop_proof_sha256": "e" * 64},
        {"actor": "github:other:8"},
        {"event_id": "different"},
        {"session_epoch": "old"},
        {"node_id": "other"},
        {"job_id": "other"},
        {"proof_version": True},
        {"fenced_at": "2026-10-03T11:00:00Z"},
    ],
)
def test_retirement_wire_certificate_cannot_change_authority_or_metering(changes):
    proof = stop_proof()
    with pytest.raises(WorkerProtocolError):
        validate_retirement_certificate(
            {**retirement(proof), **changes},
            **quarantine_payload(proof),
            node_id="node",
            session_epoch="epoch",
            run_id="ghost",
        )


def test_authenticated_quarantine_transport_releases_only_retired_ghost(tmp_path):
    async def scenario():
        path = tmp_path / "journal.sqlite"
        with OperationJournal(path, node_id="node", session_epoch="epoch") as journal:
            journal.claim_run(run_id="ghost", start_hash=stop_proof()["start_hash"])
        journal = OperationJournal(path, node_id="node", session_epoch="epoch")
        driver = FencedDriver()
        server = WorkerServer(
            "127.0.0.1",
            0,
            node_id="node",
            session_epoch="epoch",
            secret=SECRET,
            drivers=[driver],
            journal=journal,
            allow_insecure_loopback=True,
        )
        host, port = await server.start()
        client = RemoteWorkerClient(
            host,
            port,
            node_id="node",
            session_epoch="epoch",
            secret=SECRET,
            allow_insecure_loopback=True,
        )
        backend = RemoteWorkerBackend(client, name="remote", expected_driver="fake")
        try:
            assert (await backend.heartbeat())["active_runs"] == 1
            proof = stop_proof()
            certificate = await backend.quarantine_run(
                "ghost",
                actor=proof["actor"],
                event_id=proof["event_id"],
                stop_proof=proof,
            )
            assert certificate == retirement(proof)
            assert (await backend.heartbeat())["active_runs"] == 0
            with pytest.raises(WorkerStartUncertainError):
                await client.start(
                    "fake", contract(), run_id="ghost", request_id="old-start"
                )
            handle = await client.start("fake", contract(), run_id="fresh")
            assert handle.id == "fresh-handle" and driver.start_calls == 1
            assert journal.load_run_result(run_id="ghost") is None
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())
