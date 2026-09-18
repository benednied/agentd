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
    store.close()
