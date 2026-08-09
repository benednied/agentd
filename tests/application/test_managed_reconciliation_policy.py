from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import (
    AllocationState,
    JobState,
    QoSClass,
    QuotaUnit,
    ReservationState,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    DriverSession,
    EffortEstimate,
    ExecutionContract,
    HarnessCapabilities,
    Job,
    ProviderQuotaSnapshot,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    RunHandle,
    RunObservation,
    RunRecord,
    RunResult,
    TokenUsage,
    UsageSample,
    WorkerNode,
    WorkspaceLease,
    utc_now,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


class ScriptedManagedDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        handles = count(1)
        super().__init__(
            capabilities=HarnessCapabilities(
                name="codex",
                models=frozenset({"standard"}),
                features=frozenset({"checkpointing", "steering"}),
                checkpointing=True,
                steering=True,
            ),
            id_factory=lambda: f"codex-handle-{next(handles)}",
        )
        self.observations: dict[str, RunObservation | None] = {}
        self.continuations: list[tuple[str, str]] = []

    async def start_managed(
        self,
        run_id: str,
        execution: ExecutionContract,
    ) -> RunHandle:
        self.observations[run_id] = None
        return await super().start(execution)

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = "Resume from the durable thread and workspace.",
    ) -> RunHandle:
        del recovery_instruction
        self.observations.setdefault(run_id, None)
        return await super().start(execution)

    def observe(self, run_id: str) -> RunObservation | None:
        return self.observations.get(run_id)

    async def continue_turn(self, run_id: str, instruction: str) -> RunHandle:
        self.continuations.append((run_id, instruction))
        previous = self.observations[run_id]
        assert previous is not None
        self.observations[run_id] = replace(
            previous,
            turn_id=f"repair-{len(self.continuations)}",
            cursor=f"repair-{len(self.continuations)}-started",
            terminal=False,
            usage=None,
            cumulative_quota=None,
            run_state=RunState.RUNNING,
            result=None,
        )
        return RunHandle(id=run_id, driver="codex", external_id="thread")


@dataclass(slots=True)
class RetainingWorkspaceManager:
    leases: dict[str, WorkspaceLease] = field(default_factory=dict)
    releases: list[str] = field(default_factory=list)

    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        lease = WorkspaceLease(
            id=f"workspace-{job.id}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
            created_at=NOW,
        )
        self.leases[lease.id] = lease
        return lease

    def is_available(self, lease: WorkspaceLease) -> bool:
        return self.leases.get(lease.id) == lease

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        self.releases.append(lease.id)
        released = replace(
            lease,
            state=WorkspaceState.RELEASED,
            commit="f" * 40,
            released_at=NOW,
        )
        self.leases[lease.id] = released
        return released

    def current_commit(self, lease: WorkspaceLease) -> str:
        return lease.commit or "f" * 40


@dataclass(slots=True)
class ReconciliationRig:
    store: SQLiteStateStore
    workspaces: RetainingWorkspaceManager
    driver: ScriptedManagedDriver
    coordinator: SchedulerCoordinator
    plane: ControlPlane


@pytest.fixture
def make_reconciliation_rig() -> Iterator[Callable[..., ReconciliationRig]]:
    stores: list[SQLiteStateStore] = []

    def factory(*, enforce_provider_policy: bool = False) -> ReconciliationRig:
        store = SQLiteStateStore()
        stores.append(store)
        workspaces = RetainingWorkspaceManager()
        driver = ScriptedManagedDriver()
        coordinator = SchedulerCoordinator(
            store,
            workspaces,
            DriverRegistry((driver,)),
            enforce_codex_account_policy=enforce_provider_policy,
        )
        return ReconciliationRig(
            store=store,
            workspaces=workspaces,
            driver=driver,
            coordinator=coordinator,
            plane=ControlPlane(store, coordinator=coordinator),
        )

    yield factory

    for store in stores:
        store.close()


def _job(
    *,
    job_id: str = "managed-job",
    qos: QoSClass = QoSClass.NORMAL,
) -> Job:
    return Job(
        id=job_id,
        project="managed-reconciliation",
        repository="/repositories/example",
        objective="exercise managed reconciliation",
        quota_budget=QuotaBudget(
            implementation=50_000,
            maximum=100_000,
            unit=QuotaUnit.TOKENS,
        ),
        effort=EffortEstimate(p50=5, p90=10, p99=20),
        qos=qos,
        preferred_harnesses=("codex",),
        allowed_harnesses=("codex",),
        resources=ResourceVector(cpu=1, ram_gb=1),
        created_at=NOW,
        updated_at=NOW,
    )


def _register_capacity(rig: ReconciliationRig) -> None:
    rig.plane.register_quota_pool(
        QuotaPool(
            id="default",
            provider="openai-codex-chatgpt",
            remaining=500_000,
            unit=QuotaUnit.TOKENS,
            updated_at=NOW,
        )
    )
    rig.plane.register_node(
        WorkerNode(
            id="node",
            labels={"os": "linux"},
            capacity=ResourceVector(cpu=4, ram_gb=8),
            harnesses=frozenset({"codex"}),
            updated_at=NOW,
        )
    )


def _dispatch(rig: ReconciliationRig, job: Job) -> RunRecord | None:
    rig.plane.submit(job)
    return asyncio.run(rig.coordinator.dispatch_next())


def _usage(run: RunRecord, cumulative: int, sequence: int) -> UsageSample:
    return UsageSample(
        id=f"sample-{run.id}-{sequence}",
        run_id=run.id,
        thread_id="thread",
        turn_id="turn",
        sequence=sequence,
        cumulative_quota=float(cumulative),
        unit=QuotaUnit.TOKENS,
        source="codex-app-server",
        tokens=TokenUsage(input_tokens=cumulative),
        observed_at=NOW + timedelta(seconds=sequence),
    )


def _observation(
    run: RunRecord,
    *,
    terminal: bool,
    telemetry_valid: bool = True,
    cumulative: int = 10_000,
) -> RunObservation:
    usage = TokenUsage(input_tokens=cumulative) if telemetry_valid else None
    result = (
        RunResult(
            outcome=RunOutcome.COMPLETED,
            summary="ready for review",
            usage=usage,
        )
        if terminal
        else None
    )
    return RunObservation(
        run_id=run.id,
        thread_id="thread",
        turn_id="turn",
        cursor="terminal" if terminal else "live",
        terminal=terminal,
        telemetry_valid=telemetry_valid,
        usage=usage,
        cumulative_quota=float(cumulative) if usage is not None else None,
        unit=QuotaUnit.TOKENS,
        source="codex-app-server",
        run_state=RunState.COMPLETED if terminal else RunState.RUNNING,
        result=result,
        observed_at=NOW,
    )


def test_terminal_observation_enters_review_and_releases_execution_capacity(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.store.apply_usage_sample(_usage(run, 10_000, 1))
    rig.driver.observations[run.id] = _observation(run, terminal=True)

    finalized = asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))

    assert [job.state for job in finalized] == [JobState.REVIEW]
    assert rig.store.get_job(run.job_id).state is JobState.REVIEW
    assert rig.store.get_run(run.id).state is RunState.SUSPENDED
    assert rig.store.get_allocation(run.allocation_id).state is AllocationState.RELEASED
    assert (
        rig.store.get_reservation(run.reservation_id).state is ReservationState.RELEASED
    )
    assert rig.store.get_reservation(run.reservation_id).consumed == 10_000
    workspace = rig.store.get_workspace(run.workspace_id)
    assert workspace.state is WorkspaceState.LEASED
    assert rig.workspaces.releases == []


def test_invalid_terminal_telemetry_holds_quota_in_metering_pending(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.driver.observations[run.id] = _observation(
        run,
        terminal=True,
        telemetry_valid=False,
    )

    finalized = asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))

    assert [job.state for job in finalized] == [JobState.METERING_PENDING]
    assert rig.store.get_run(run.id).state is RunState.SUSPENDED
    assert rig.store.get_allocation(run.allocation_id).state is AllocationState.RELEASED
    reservation = rig.store.get_reservation(run.reservation_id)
    assert reservation.state is ReservationState.METERING_PENDING
    assert rig.store.get_quota_pool("default").reserved == reservation.outstanding
    assert rig.store.get_workspace(run.workspace_id).state is WorkspaceState.LEASED


def test_review_repair_request_starts_same_thread_with_fresh_capacity(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.store.apply_usage_sample(_usage(run, 10_000, 1))
    terminal = _observation(run, terminal=True)
    rig.driver.observations[run.id] = terminal
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))
    rig.store.save_driver_session(
        DriverSession(
            id="session",
            run_id=run.id,
            driver="codex",
            thread_id=terminal.thread_id,
            turn_id=terminal.turn_id,
            observation_cursor=terminal.cursor,
            last_observation=terminal,
            active=False,
            metadata={"continuation_count": 0},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    request = rig.plane.request_repair(run.job_id, "Fix the validation failure.")

    finalized = asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))

    assert finalized == ()
    assert rig.store.get_job(run.job_id).state is JobState.RUNNING
    repaired = rig.store.get_run(run.id)
    assert repaired.state is RunState.RUNNING
    assert repaired.id == run.id
    assert repaired.reservation_id != run.reservation_id
    assert repaired.allocation_id != run.allocation_id
    assert rig.driver.continuations == [
        (run.id, "Fix the validation failure."),
    ]
    assert rig.store.get_run_command_ack(request.id) is not None
    assert rig.store.latest_checkpoint(run.job_id) is not None


def test_review_rejects_a_third_repair_turn(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.store.apply_usage_sample(_usage(run, 10_000, 1))
    terminal = _observation(run, terminal=True)
    rig.driver.observations[run.id] = terminal
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))
    rig.store.save_driver_session(
        DriverSession(
            id="session-at-limit",
            run_id=run.id,
            driver="codex",
            thread_id=terminal.thread_id,
            turn_id=terminal.turn_id,
            observation_cursor=terminal.cursor,
            last_observation=terminal,
            active=False,
            metadata={"continuation_count": 2},
            created_at=NOW,
            updated_at=NOW,
        )
    )

    with pytest.raises(ValueError, match="two-repair-turn limit"):
        rig.plane.request_repair(run.job_id, "Try one more repair.")


def test_live_usage_top_up_checkpoint_and_delayed_hard_cap_interrupt(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.driver.observations[run.id] = _observation(run, terminal=False)

    rig.store.apply_usage_sample(_usage(run, 40_000, 1))
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))
    assert rig.store.get_reservation(run.reservation_id).amount == 75_000

    rig.store.apply_usage_sample(_usage(run, 60_000, 2))
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))
    assert rig.store.get_reservation(run.reservation_id).amount == 100_000

    rig.store.apply_usage_sample(_usage(run, 90_000, 3))
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW))
    asyncio.run(rig.coordinator.reconcile_managed_runs(at=NOW + timedelta(seconds=1)))
    commands = rig.store.list_run_commands(run.id)
    assert [command.action for command in commands] == ["checkpoint"]
    assert commands[0].id == f"usage-maximum-checkpoint:{run.job_id}"

    cap_sample = _usage(run, 100_000, 4)
    rig.store.apply_usage_sample(cap_sample)
    asyncio.run(
        rig.coordinator.reconcile_managed_runs(
            at=cap_sample.observed_at + timedelta(seconds=119, milliseconds=999)
        )
    )
    assert [command.action for command in rig.store.list_run_commands(run.id)] == [
        "checkpoint"
    ]

    asyncio.run(
        rig.coordinator.reconcile_managed_runs(
            at=cap_sample.observed_at + timedelta(seconds=120)
        )
    )
    asyncio.run(
        rig.coordinator.reconcile_managed_runs(
            at=cap_sample.observed_at + timedelta(seconds=121)
        )
    )
    commands = rig.store.list_run_commands(run.id)
    assert [command.action for command in commands] == ["checkpoint", "interrupt"]
    assert (
        commands[-1].payload["deadline"]
        == (cap_sample.observed_at + timedelta(seconds=120)).isoformat()
    )


def test_late_hard_cap_reconciliation_orders_checkpoint_before_interrupt(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job())
    assert run is not None
    rig.driver.observations[run.id] = _observation(run, terminal=False)
    cap_sample = _usage(run, 100_000, 1)
    rig.store.apply_usage_sample(cap_sample)

    asyncio.run(
        rig.coordinator.reconcile_managed_runs(
            at=cap_sample.observed_at + timedelta(seconds=121)
        )
    )

    assert [
        command.action for command in rig.store.list_pending_run_commands(run.id)
    ] == ["checkpoint", "interrupt"]


@pytest.mark.parametrize(
    ("qos", "used", "reached"),
    (
        (QoSClass.NORMAL, 90, False),
        (QoSClass.INTERACTIVE, 10, True),
    ),
)
def test_provider_pressure_enqueues_exactly_once_checkpoint_for_active_runs(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
    qos: QoSClass,
    used: float,
    reached: bool,
) -> None:
    rig = make_reconciliation_rig()
    _register_capacity(rig)
    run = _dispatch(rig, _job(qos=qos))
    assert run is not None
    rig.driver.observations[run.id] = _observation(run, terminal=False)
    snapshot = ProviderQuotaSnapshot(
        id="provider-pressure",
        pool_id="default",
        bucket_id="codex",
        primary_used_percent=used,
        reached=reached,
        observed_at=NOW,
    )

    asyncio.run(rig.coordinator.reconcile_managed_runs(snapshot, at=NOW))
    asyncio.run(rig.coordinator.reconcile_managed_runs(snapshot, at=NOW))

    commands = rig.store.list_run_commands(run.id)
    assert len(commands) == 1
    assert commands[0].action == "checkpoint"
    assert commands[0].id.startswith(f"provider-quota-checkpoint:{run.id}:")


@pytest.mark.parametrize(
    ("qos", "used", "reached", "credits_exhausted", "admitted"),
    (
        (QoSClass.NORMAL, 74.999, False, None, True),
        (QoSClass.SPECULATIVE, 75, False, None, False),
        (QoSClass.NORMAL, 75, False, None, True),
        (QoSClass.NORMAL, 90, False, None, False),
        (QoSClass.INTERACTIVE, 90, False, None, True),
        (QoSClass.INTERACTIVE, 10, True, None, False),
        (QoSClass.BLOCKER, 10, False, True, False),
    ),
)
def test_provider_snapshot_gates_codex_admission(
    make_reconciliation_rig: Callable[..., ReconciliationRig],
    qos: QoSClass,
    used: float,
    reached: bool,
    credits_exhausted: bool | None,
    admitted: bool,
) -> None:
    rig = make_reconciliation_rig(enforce_provider_policy=True)
    _register_capacity(rig)
    snapshot = ProviderQuotaSnapshot(
        id="admission-snapshot",
        pool_id="default",
        bucket_id="codex",
        primary_used_percent=used,
        reached=reached,
        credits_exhausted=credits_exhausted,
        observed_at=utc_now(),
    )
    rig.store.append_provider_quota_snapshot(snapshot)

    run = _dispatch(rig, _job(qos=qos))

    assert (run is not None) is admitted
