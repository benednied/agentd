from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import RunState, TailAction
from agentd.domain.models import (
    EffortEstimate,
    ExecutionContract,
    Job,
    ProviderQuotaSnapshot,
    QuotaBudget,
    RunHandle,
    RunObservation,
    RunRecord,
)
from agentd.runtime.governor import (
    ProviderStopPolicy,
    evaluate_provider_stop,
    observed_effort,
    provider_stop_command,
    tail_governor_command,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _snapshot(used: float | None, **overrides: object) -> ProviderQuotaSnapshot:
    values: dict[str, object] = {
        "id": "snapshot-1",
        "pool_id": "codex",
        "bucket_id": "five-hour",
        "primary_used_percent": used,
        "primary_reset_at": NOW + timedelta(hours=1),
        "observed_at": NOW,
    }
    values.update(overrides)
    return ProviderQuotaSnapshot(**values)  # type: ignore[arg-type]


def _job(*, unit: str = "agent-minutes") -> Job:
    return Job(
        id="job-1",
        project="example",
        repository="/repo",
        objective="work",
        quota_budget=QuotaBudget(10),
        effort=EffortEstimate(5, 10, 20, unit=unit),
    )


def _run(*, started_at: datetime = NOW - timedelta(minutes=11)) -> RunRecord:
    contract = ExecutionContract(
        job_id="job-1",
        objective="work",
        scope="repo",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=("/workspace",),
        checkpoint_expectations="checkpoint",
        coordination_mechanisms=(),
        completion_protocol="complete",
        working_directory="/workspace",
        environment={},
        model_class="standard",
    )
    return RunRecord(
        id="run-1",
        job_id="job-1",
        node_id="node-1",
        workspace_id="workspace-1",
        reservation_id="reservation-1",
        allocation_id="allocation-1",
        driver="fake",
        backend="local",
        contract=contract,
        handle=RunHandle(id="handle-1", driver="fake"),
        state=RunState.RUNNING,
        started_at=started_at,
    )


@pytest.mark.parametrize(
    "value",
    [0, -0.1, 0.01, 0.03, 1.1, float("inf")],
)
def test_provider_stop_policy_rejects_invalid_fractions(value: float) -> None:
    with pytest.raises(ValueError, match=r"exactly 0\.02"):
        ProviderStopPolicy(value)


def test_two_percent_boundary_is_inclusive_and_command_is_stable() -> None:
    below = evaluate_provider_stop(_snapshot(97.99), at=NOW)
    boundary = evaluate_provider_stop(_snapshot(98), at=NOW)
    exhausted = evaluate_provider_stop(_snapshot(100, reached=True), at=NOW)

    assert below.stop is False
    assert boundary.stop is True
    assert boundary.remaining_fraction == pytest.approx(0.02)
    assert exhausted.stop is True

    first = provider_stop_command(_snapshot(98), run_id="run-1", at=NOW)
    replay = provider_stop_command(_snapshot(99), run_id="run-1", at=NOW)
    assert first is not None and replay is not None
    assert first.id == replay.id
    assert first.action == "interrupt"


def test_stale_or_percentage_free_snapshot_does_not_interrupt_active_work() -> None:
    stale = _snapshot(100, observed_at=NOW - timedelta(minutes=6))
    unknown = _snapshot(None)

    assert provider_stop_command(stale, run_id="run-1", at=NOW) is None
    assert provider_stop_command(unknown, run_id="run-1", at=NOW) is None


def test_tail_governor_uses_wall_clock_agent_minutes_and_escalates() -> None:
    decision, command = tail_governor_command(_job(), _run(), at=NOW) or (None, None)
    assert decision is not None and decision.action is TailAction.REESTIMATE
    assert command is not None
    assert command.id == "tail-governor:run-1:reestimate"
    assert command.action == "steer"

    decision, command = tail_governor_command(
        _job(),
        _run(started_at=NOW - timedelta(minutes=16)),
        at=NOW,
    ) or (None, None)
    assert decision is not None and decision.action is TailAction.CHECKPOINT_REPLAN
    assert command is not None and command.action == "checkpoint"

    decision, command = tail_governor_command(
        _job(),
        _run(started_at=NOW - timedelta(minutes=21)),
        at=NOW,
    ) or (None, None)
    assert decision is not None
    assert decision.action is TailAction.CONVERT_TO_HORS_CATEGORIE
    assert command is not None
    assert command.payload["tail_action"] == "convert-to-hors-categorie"


def test_driver_effort_metadata_requires_exact_unit_and_valid_value() -> None:
    run = _run(started_at=NOW - timedelta(minutes=1))
    observation = RunObservation(
        run_id=run.id,
        thread_id="thread-1",
        turn_id="turn-1",
        cursor="1",
        metadata={"effort_consumed": 30, "effort_unit": "story-points"},
        observed_at=NOW,
    )

    assert (
        observed_effort(_job(unit="story-points"), run, at=NOW, observation=observation)
        == 30
    )
    assert (
        observed_effort(_job(unit="tokens"), run, at=NOW, observation=observation)
        is None
    )


def test_continue_and_unknown_effort_do_not_emit_commands() -> None:
    decision, command = tail_governor_command(
        _job(),
        _run(started_at=NOW - timedelta(minutes=5)),
        at=NOW,
    ) or (None, None)
    assert decision is not None and decision.action is TailAction.CONTINUE
    assert command is None
    assert tail_governor_command(_job(unit="story-points"), _run(), at=NOW) is None
