"""Physical retirement requires protected host evidence and no provider work."""

import asyncio
import hashlib
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agentd.domain.models import utc_now
from agentd.lifecycle import ControllerLock
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.coding import CodingHarnessDriver
from agentd.workers.coding_runtime import _ContainedCodexDriver
from agentd.workers.operations import OperationError
from agentd.workers.remote_protocol import canonical_json, payload_hash


@pytest.fixture
def quarantine_rig(tmp_path):
    (tmp_path / "state").mkdir(mode=0o700)
    store = SQLiteStateStore(tmp_path / "state" / "sdk.sqlite")
    proof = {
        "proof_version": 1,
        "job_id": "job",
        "node_id": "worker",
        "session_epoch": "epoch",
        "run_id": "run",
        "start_hash": "a" * 64,
        "actor": "owner",
        "event_id": "comment:123",
        "container_id": "b" * 64,
        "stopped_at": utc_now().isoformat(),
        "running": False,
        "pid": 0,
    }
    directory = tmp_path / "state" / "quarantine-stops"
    directory.mkdir(mode=0o700)
    name = hashlib.sha256(
        canonical_json({"run_id": "run", "event_id": proof["event_id"]})
    ).hexdigest()
    path = directory / f"{name}.json"
    path.write_bytes(canonical_json(proof))
    path.chmod(0o600)
    owner = ControllerLock(tmp_path / "state" / "worker.sqlite")
    owner.acquire()
    driver = _ContainedCodexDriver(
        SimpleNamespace(_live={}), store, model="test", worker_owner=owner
    )
    rig = SimpleNamespace(
        store=store, proof=proof, path=path, owner=owner, driver=driver
    )
    try:
        yield rig
    finally:
        owner.release()
        store.close()


def prove(rig, proof=None):
    return rig.driver.prove_physical_quarantine(
        "run",
        None,
        None,
        actor="owner",
        event_id="comment:123",
        stop_proof=proof or rig.proof,
        start_hash="a" * 64,
    )


def test_protected_quarantine_replays_immutable_certificate(quarantine_rig, tmp_path):
    rig = quarantine_rig
    coding = CodingHarnessDriver(tmp_path / "leases", {}, {"codex": rig.driver})

    async def retire():
        return await coding.quarantine_run(
            "run",
            actor="owner",
            event_id="comment:123",
            stop_proof=rig.proof,
            start_hash="a" * 64,
        )

    first = asyncio.run(retire())
    marker = coding._lease("run") / "quarantine.json"
    original = marker.read_bytes()
    assert first["physical_retired"] is first["metering_unknown"] is True
    assert first["stop_proof_sha256"] == payload_hash(rig.proof)
    assert len(first) == 14
    rig.owner.release()
    rig.owner.acquire()
    assert asyncio.run(retire()) == first
    assert marker.read_bytes() == original
    assert not (coding._lease("run") / "result.json").exists()
    assert rig.store.list_runs() == []
    assert rig.store.list_quota_pools() == []
    assert rig.store.list_usage_samples("run") == []


@pytest.mark.parametrize(
    "violation",
    [
        "missing",
        "mode",
        "different",
        "future",
        "mtime",
        "live",
        "owner",
        "pid",
    ],
)
def test_quarantine_rejects_unproven_stop(quarantine_rig, violation):
    rig = quarantine_rig
    proof = dict(rig.proof)
    if violation == "missing":
        rig.path.unlink()
    elif violation == "mode":
        rig.path.chmod(0o644)
    elif violation == "different":
        proof["container_id"] = "c" * 64
    elif violation == "future":
        proof["stopped_at"] = (utc_now() + timedelta(hours=1)).isoformat()
    elif violation == "mtime":
        future = (utc_now() + timedelta(hours=1)).timestamp()
        os.utime(rig.path, (future, future))
    elif violation == "live":
        rig.driver._supervisor._live["run"] = object()
    elif violation == "owner":
        rig.owner.release()
    elif violation == "pid":
        proof["pid"] = False
    with pytest.raises((OperationError, FileNotFoundError)):
        prove(rig, proof)
    assert rig.store.list_runs() == []
