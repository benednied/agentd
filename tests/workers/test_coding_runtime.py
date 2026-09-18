from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.workers import coding_runtime
from agentd.workers.operations import OperationError


def test_verified_factory_cannot_skip_runtime_proof(tmp_path, monkeypatch):
    calls = []

    async def rejected(self, argv, **kwargs):
        calls.append((argv, kwargs))
        raise OperationError("preflight failed")

    monkeypatch.setattr(coding_runtime.SubprocessCommandRunner, "run", rejected)
    with pytest.raises(OperationError, match="preflight failed"):
        asyncio.run(
            coding_runtime.create_verified_coding_sdk(
                Path("/home/bened/.local/state/agentd/test.sqlite"),
                environment={
                    "CODEX_HOME": "/home/bened/.local/share/agentd/codex-home"
                },
            )
        )
    assert len(calls) == 1
    assert calls[0][0] == (
        "/opt/agentd/venv/bin/python",
        "/opt/agentd/security/runtime_sandbox_probe.py",
    )


@pytest.mark.parametrize(
    "state,environment",
    [
        ("/tmp/model-readable.sqlite", {}),
        ("/home/bened/.local/state/agentd/test.sqlite", {"GH_TOKEN": "do-not-pass"}),
        (
            "/home/bened/.local/state/agentd/test.sqlite",
            {"CODEX_HOME": "/tmp/readable"},
        ),
    ],
)
def test_verified_factory_rejects_unprotected_runtime(state, environment):
    with pytest.raises(OperationError):
        asyncio.run(
            coding_runtime.create_verified_coding_sdk(
                Path(state),
                environment=environment,
            )
        )


def test_worker_envelope_records_real_usage_without_admitting_jobs(
    tmp_path, monkeypatch
):
    from agentd.coding.models import CodingWorkOrder, RepositoryProfile
    from agentd.domain.enums import QuotaUnit
    from agentd.domain.models import (
        CodingOperation,
        ExecutionContract,
        RunHandle,
        TokenUsage,
        UsageSample,
    )
    from agentd.state.sqlite import SQLiteStateStore

    store = SQLiteStateStore(tmp_path / "worker.sqlite")
    profile = RepositoryProfile(
        "p", "v1", "test/repo", "https://github.com/test/repo.git"
    )
    order = CodingWorkOrder(
        "job",
        "test/repo",
        "p",
        "v1",
        profile.digest,
        "a" * 40,
        "b" * 64,
        "change",
        "codex",
        "real-account",
        100,
        200,
        30,
    )
    contract = ExecutionContract(
        "job",
        "change",
        "workspace",
        (),
        {},
        "implementer",
        (str(tmp_path),),
        "",
        (),
        "",
        str(tmp_path),
        {},
        "standard",
        operation=CodingOperation(order),
    )

    async def start(self, run_id, execution):
        assert store.get_run(run_id).contract == execution
        return RunHandle(run_id, "codex")

    monkeypatch.setattr(coding_runtime.CodexSdkDriver, "start_managed", start)
    driver = coding_runtime._ContainedCodexDriver(None, store, model="test-model")
    handle = asyncio.run(driver.start_managed("run", contract))
    assert handle.id == "run"
    application = store.apply_usage_sample(
        UsageSample(
            "run",
            "thread",
            "turn",
            1,
            20,
            unit=QuotaUnit.TOKENS,
            tokens=TokenUsage(input_tokens=10, output_tokens=10),
        )
    )
    assert application.delta == 20
    assert application.pool.id == "execution-envelope:run"
    assert application.pool.provider == "controller-execution-envelope"
    assert application.reservation.consumed == 20
    from dataclasses import replace

    from agentd.state.base import EntityNotFoundError

    second = replace(
        contract,
        job_id="job-2",
        operation=CodingOperation(replace(order, job_id="job-2")),
    )
    first_node = store.get_node("worker-local")
    asyncio.run(driver.start_managed("run-2", second))
    assert store.get_node("worker-local") == first_node
    assert len(store.list_nodes()) == 1
    assert store.get_quota_pool("execution-envelope:run").remaining == 180
    assert store.get_reservation(application.reservation.id).consumed == 20

    from agentd.domain.enums import JobState
    from agentd.domain.models import StateTransition

    # Reproduce the old partial state left at save_node, before any SDK call.
    legacy_order = replace(order, job_id="legacy-job")
    legacy_job = replace(
        store.get_job("job"),
        id=legacy_order.job_id,
        quota_budget=replace(
            store.get_job("job").quota_budget, pool_id="execution-envelope:legacy-run"
        ),
    )
    store.create_job(
        legacy_job,
        StateTransition(legacy_job.id, None, JobState.RUNNING, "legacy setup"),
    )
    legacy_lease = replace(
        store.get_workspace("run"), id="legacy-run", job_id=legacy_job.id
    )
    store.save_workspace(legacy_lease, expected=None)
    proof = driver.prove_preparation_failure("legacy-run", legacy_order, legacy_lease)
    assert proof.metadata["provider_started"] is False
    assert proof.consumed_quota == proof.usage.total_tokens == 0
    assert store.get_workspace("legacy-run") == legacy_lease
    with pytest.raises(OperationError, match="cannot be ruled out"):
        driver.prove_preparation_failure("run", order, store.get_workspace("run"))

    def fail_run_insert(*args, **kwargs):
        raise RuntimeError("forced failure after all preparation inserts")

    monkeypatch.setattr(store, "_save_run_in_transaction", fail_run_insert)
    third = replace(
        contract,
        job_id="job-3",
        operation=CodingOperation(replace(order, job_id="job-3")),
    )
    with pytest.raises(coding_runtime.CodingPreparationError):
        asyncio.run(driver.start_managed("run-3", third))
    for lookup, identity in (
        (store.get_job, "job-3"),
        (store.get_run, "run-3"),
        (store.get_workspace, "run-3"),
        (store.get_quota_pool, "execution-envelope:run-3"),
    ):
        with pytest.raises(EntityNotFoundError):
            lookup(identity)
    assert len(store.list_nodes()) == 1
    assert store.get_quota_pool("execution-envelope:run").remaining == 180
    store.close()
