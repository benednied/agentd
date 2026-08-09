from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentd.domain.enums import (
    JobState,
    QuotaUnit,
    ReservationState,
    RunOutcome,
    RunState,
)
from agentd.domain.models import (
    DriverSession,
    ExecutionContract,
    Job,
    ProviderQuotaSnapshot,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    RunCommandAck,
    RunHandle,
    RunObservation,
    RunRecord,
    RunResult,
    TokenUsage,
    UsageSample,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import initial_transition, transition_job
from agentd.runtime.accounts import maximum_checkpoint_command
from agentd.runtime.quota import QuotaManager, QuotaMaximumExceeded
from agentd.runtime.resources import ResourceManager
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _contract(job: Job) -> ExecutionContract:
    return ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="usage test",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=("/worktree",),
        checkpoint_expectations="checkpoint safely",
        coordination_mechanisms=(),
        completion_protocol="report",
        working_directory="/worktree",
        environment={},
        model_class="standard",
    )


def _runtime(
    make_job: Callable[..., Job],
    *,
    implementation: float = 10,
    maximum: float = 20,
    remaining: float = 100,
    path: str | Path = ":memory:",
) -> tuple[SQLiteStateStore, Job, RunRecord]:
    job = make_job(
        id="usage-job",
        state=JobState.RUNNING,
        quota_budget=QuotaBudget(
            implementation=implementation,
            maximum=maximum,
            unit=QuotaUnit.TOKENS,
        ),
    )
    store = SQLiteStateStore(path)
    store.create_job(job, initial_transition(job, reason="already running"))
    store.save_quota_pool(
        QuotaPool(
            id="default",
            provider="codex",
            remaining=remaining,
            unit=QuotaUnit.TOKENS,
        )
    )
    node = WorkerNode(
        id="node",
        labels={},
        capacity=ResourceVector(cpu=4, ram_gb=8),
        harnesses=frozenset({"codex"}),
    )
    store.save_node(node)
    reservation = QuotaManager(store).reserve(job)
    allocation = ResourceManager(store).allocate(job, node)
    workspace = WorkspaceLease(
        id="workspace",
        job_id=job.id,
        repository=job.repository,
        branch="agentd/usage-job",
        working_directory="/worktree",
        base_ref=job.base_ref,
    )
    store.save_workspace(workspace)
    run = RunRecord(
        id="run",
        job_id=job.id,
        node_id=node.id,
        workspace_id=workspace.id,
        reservation_id=reservation.id,
        allocation_id=allocation.id,
        driver="codex",
        backend="local",
        contract=_contract(job),
        handle=RunHandle(id="handle", driver="codex"),
        state=RunState.RUNNING,
        started_at=NOW,
    )
    store.save_run(run)
    return store, job, run


def _sample(
    cumulative: int,
    *,
    sequence: int = 1,
    turn_id: str = "turn-1",
    final: bool = False,
) -> UsageSample:
    return UsageSample(
        id=f"sample-{turn_id}-{sequence}",
        run_id="run",
        thread_id="thread-1",
        turn_id=turn_id,
        sequence=sequence,
        cumulative_quota=float(cumulative),
        unit=QuotaUnit.TOKENS,
        source="codex-app-server",
        tokens=TokenUsage(input_tokens=cumulative),
        observed_at=NOW + timedelta(seconds=sequence),
        final=final,
    )


def test_usage_application_is_atomic_monotonic_and_deduplicated(
    make_job: Callable[..., Job],
) -> None:
    store, job, run = _runtime(make_job, maximum=12)
    manager = QuotaManager(store)
    first_sample = _sample(4)

    first = manager.apply_codex_usage(first_sample)
    duplicate = manager.apply_codex_usage(first_sample)
    second = manager.apply_codex_usage(_sample(7, sequence=2))

    assert first.delta == 4
    assert first.sample.delta == 4
    assert not first.duplicate
    assert duplicate.delta == 0
    assert duplicate.duplicate
    assert second.delta == 3
    assert second.job_consumed == 7
    assert store.get_reservation(run.reservation_id).consumed == 7
    pool = store.get_quota_pool(job.quota_budget.pool_id)
    assert pool.remaining == 93
    assert pool.reserved == 3
    assert [sample.delta for sample in store.list_usage_samples(run.id)] == [4, 3]

    with pytest.raises(ConcurrentStateError, match="different reading"):
        manager.apply_codex_usage(
            replace(
                first_sample,
                cumulative_quota=5,
                tokens=TokenUsage(input_tokens=5),
            )
        )
    with pytest.raises(ValueError, match="positive delta"):
        manager.apply_codex_usage(_sample(6, sequence=3))

    assert store.get_reservation(run.reservation_id).consumed == 7
    assert len(store.list_usage_samples(run.id)) == 2


def test_codex_maximum_is_cumulative_across_turns_and_charged_before_error(
    make_job: Callable[..., Job],
) -> None:
    store, _job, run = _runtime(make_job, implementation=4, maximum=10)
    manager = QuotaManager(store)

    manager.apply_codex_usage(_sample(6, turn_id="turn-1"))
    with pytest.raises(QuotaMaximumExceeded) as captured:
        manager.apply_codex_usage(_sample(4, turn_id="turn-2"))

    assert captured.value.application.job_consumed == 10
    assert captured.value.application.maximum == 10
    assert captured.value.application.maximum_exceeded
    assert store.get_reservation(run.reservation_id).consumed == 10
    assert store.get_quota_pool("default").remaining == 90
    assert len(store.list_usage_samples(run.id)) == 2


def test_store_derives_configured_maximum_for_supervisor_style_application(
    make_job: Callable[..., Job],
) -> None:
    store, _job, _run = _runtime(make_job, implementation=4, maximum=5)

    application = store.apply_usage_sample(_sample(5))

    assert application.maximum == 5
    assert application.maximum_exceeded


def test_typed_token_counters_must_match_normalized_quota() -> None:
    with pytest.raises(ValueError, match="counters"):
        UsageSample(
            run_id="run",
            thread_id="thread",
            turn_id="turn",
            sequence=1,
            cumulative_quota=2,
            unit=QuotaUnit.TOKENS,
            tokens=TokenUsage(input_tokens=3),
        )


def test_usage_overage_records_debt_without_double_charge_on_release(
    make_job: Callable[..., Job],
) -> None:
    store, _job, run = _runtime(
        make_job,
        implementation=4,
        maximum=20,
        remaining=5,
    )
    manager = QuotaManager(store)

    applied = manager.apply_codex_usage(_sample(8))
    released = manager.release(run.reservation_id, consumed=8)

    assert applied.debt_incurred == 3
    assert released.consumed == 8
    assert released.debt == 3
    pool = store.get_quota_pool("default")
    assert pool.remaining == 0
    assert pool.reserved == 0
    assert pool.debt == 3


def test_top_up_after_sparse_overage_reserves_only_new_future_headroom(
    make_job: Callable[..., Job],
) -> None:
    store, _job, run = _runtime(
        make_job,
        implementation=10,
        maximum=50,
        remaining=100,
    )
    manager = QuotaManager(store)
    manager.apply_codex_usage(_sample(15))

    topped_up = manager.top_up(run.reservation_id, 25)

    assert topped_up.amount == 35
    assert topped_up.consumed == 15
    assert topped_up.outstanding == 20
    assert store.get_quota_pool("default").reserved == 20


def test_top_up_and_metering_final_settlement(
    make_job: Callable[..., Job],
) -> None:
    store, job, run = _runtime(make_job, maximum=30)
    manager = QuotaManager(store)
    manager.apply_codex_usage(_sample(8))

    topped_up = manager.top_up(run.reservation_id, 5)
    assert topped_up.amount == 15
    assert store.get_quota_pool("default").reserved == 7

    pending = manager.begin_metering(job.id)
    assert pending.state is ReservationState.METERING_PENDING
    assert store.get_job(job.id).state is JobState.METERING_PENDING

    final_sample = _sample(10, sequence=2, final=True)
    settled = manager.settle(run.reservation_id, final_sample=final_sample)
    assert settled.state is ReservationState.RELEASED
    assert settled.consumed == 10
    assert store.get_quota_pool("default").remaining == 90
    assert store.get_quota_pool("default").reserved == 0

    pending_job = store.get_job(job.id)
    review, event = transition_job(
        pending_job,
        JobState.REVIEW,
        "final telemetry accepted",
    )
    store.save_job(review, event)

    duplicate = store.apply_usage_sample(final_sample, maximum=30)
    assert duplicate.duplicate
    with pytest.raises(ValueError, match="cannot accept telemetry"):
        store.apply_usage_sample(_sample(11, sequence=3), maximum=30)


def test_terminal_usage_marker_can_repeat_the_last_cumulative_reading(
    make_job: Callable[..., Job],
) -> None:
    store, job, run = _runtime(make_job, maximum=30)
    manager = QuotaManager(store)
    manager.apply_codex_usage(_sample(8))
    manager.begin_metering(job.id)

    terminal = _sample(8, sequence=2, final=True)
    settled = manager.settle(run.reservation_id, final_sample=terminal)

    assert settled.consumed == 8
    assert store.get_quota_pool("default").remaining == 92
    samples = store.list_usage_samples(run.id)
    assert [sample.delta for sample in samples] == [8, 0]
    assert samples[-1].final


def test_driver_session_terminal_observation_and_durable_command_ack(
    make_job: Callable[..., Job],
) -> None:
    store, job, run = _runtime(make_job)
    session = DriverSession(
        id="session",
        run_id=run.id,
        driver=run.driver,
        external_id="process-1",
        created_at=NOW,
        updated_at=NOW,
    )
    store.save_driver_session(session)
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        usage=TokenUsage(input_tokens=3, output_tokens=1),
    )
    observation = RunObservation(
        run_id=run.id,
        thread_id="thread-1",
        turn_id="turn-1",
        cursor="cursor-1",
        terminal=True,
        telemetry_valid=True,
        usage=result.usage,
        unit=QuotaUnit.TOKENS,
        run_state=RunState.COMPLETED,
        result=result,
        observed_at=NOW,
    )

    terminal = store.update_observation_cursor(
        run.id,
        None,
        observation.cursor,
        observation,
    )
    assert not terminal.active
    assert terminal.last_observation == observation
    assert store.list_driver_sessions(active=False) == [terminal]
    with pytest.raises(ConcurrentStateError, match="cursor"):
        store.update_observation_cursor(run.id, None, "cursor-2")

    command = maximum_checkpoint_command(
        job_id=job.id,
        run_id=run.id,
        consumed=18,
        maximum=20,
        at=NOW,
    )
    assert command is not None
    store.enqueue_run_command(command)
    store.enqueue_run_command(replace(command, created_at=NOW + timedelta(seconds=1)))
    assert store.list_pending_run_commands(run.id) == [command]

    acknowledgement = RunCommandAck(
        id="ack",
        command_id=command.id,
        run_id=run.id,
        observation_cursor="cursor-1",
        acknowledged_at=NOW,
    )
    store.acknowledge_run_command(acknowledgement)
    assert store.list_pending_run_commands(run.id) == []
    assert store.get_run_command_ack(command.id) == acknowledgement


def test_provider_snapshots_preserve_raw_windows_and_opaque_credits(
    make_job: Callable[..., Job],
) -> None:
    store, _job, _run = _runtime(make_job)
    snapshot = ProviderQuotaSnapshot(
        id="provider-snapshot",
        pool_id="default",
        bucket_id="codex",
        primary_used_percent=72.5,
        primary_window_minutes=300,
        primary_reset_at=NOW + timedelta(hours=5),
        secondary_used_percent=20,
        secondary_window_minutes=10_080,
        secondary_reset_at=NOW + timedelta(days=7),
        credits={"opaque": {"balance": "provider-defined"}},
        rate_limit_reset_credits=[{"opaque": True}],
        observed_at=NOW,
    )

    store.append_provider_quota_snapshot(snapshot)
    store.append_provider_quota_snapshot(snapshot)

    assert store.latest_provider_quota_snapshot("default", "codex") == snapshot
    assert store.list_provider_quota_snapshots("default") == [snapshot]
    assert snapshot.reset_at == snapshot.primary_reset_at


def test_run_result_typed_usage_reads_legacy_metadata() -> None:
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        metadata={
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "output_tokens": 3,
            }
        },
    )

    assert result.usage == TokenUsage(
        input_tokens=10,
        cached_input_tokens=4,
        output_tokens=3,
    )
    assert RunResult.from_dict(result.to_dict()) == result


def test_usage_sessions_snapshots_and_commands_survive_store_restart(
    tmp_path: Path,
    make_job: Callable[..., Job],
) -> None:
    database = tmp_path / "usage.sqlite"
    store, job, run = _runtime(make_job, path=database)
    applied = store.apply_usage_sample(_sample(4))
    session = DriverSession(
        id="durable-session",
        run_id=run.id,
        driver=run.driver,
        thread_id="thread-1",
        turn_id="turn-1",
        observation_cursor="1",
        created_at=NOW,
        updated_at=NOW,
    )
    store.save_driver_session(session)
    snapshot = ProviderQuotaSnapshot(
        id="durable-snapshot",
        pool_id=job.quota_budget.pool_id,
        bucket_id="codex",
        primary_used_percent=50,
        observed_at=NOW,
    )
    store.append_provider_quota_snapshot(snapshot)
    command = maximum_checkpoint_command(
        job_id=job.id,
        run_id=run.id,
        consumed=18,
        maximum=20,
        at=NOW,
    )
    assert command is not None
    store.enqueue_run_command(command)
    acknowledgement = RunCommandAck(
        id="durable-ack",
        command_id=command.id,
        run_id=run.id,
        acknowledged_at=NOW,
    )
    store.acknowledge_run_command(acknowledgement)
    store.close()

    with SQLiteStateStore(database) as reopened:
        assert reopened.list_usage_samples(run.id) == [applied.sample]
        assert reopened.get_driver_session(run.id) == session
        assert reopened.latest_provider_quota_snapshot("default", "codex") == snapshot
        assert reopened.list_run_commands(run.id) == [command]
        assert reopened.get_run_command_ack(command.id) == acknowledgement
