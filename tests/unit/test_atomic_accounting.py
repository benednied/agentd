import sqlite3
from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    Checkpoint,
    DriverSession,
    ExecutionContract,
    Job,
    QuotaBudget,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    ResourceVector,
    ResumeCapsule,
    RunHandle,
    RunRecord,
    StateTransition,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import (
    InvalidStateTransition,
    initial_transition,
    transition_job,
)
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.runtime.resources import ResourceAllocationError, ResourceManager
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore


def _store_with_runtime(job: Job) -> tuple[SQLiteStateStore, WorkerNode]:
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    node = WorkerNode(
        id="node-1",
        labels={"os": "linux"},
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset({"fake"}),
    )
    store.save_node(node)
    return store, node


def _contract(job: Job) -> ExecutionContract:
    return ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="unit-test scope",
        acceptance_criteria=(),
        dependency_results={},
        role="implementation worker",
        allowed_filesystem_scope=("/worktree",),
        checkpoint_expectations="checkpoint safely",
        coordination_mechanisms=(),
        completion_protocol="validate and report",
        working_directory="/worktree",
        environment={},
        model_class="standard",
    )


def _run(job: Job, *, run_id: str, state: RunState) -> RunRecord:
    return RunRecord(
        id=run_id,
        job_id=job.id,
        node_id="node-1",
        workspace_id="workspace-1",
        reservation_id="reservation-1",
        allocation_id="allocation-1",
        driver="fake",
        backend="local",
        contract=_contract(job),
        handle=RunHandle(id=f"handle-{run_id}", driver="fake"),
        state=state,
    )


def _save_run_links(
    store: SQLiteStateStore,
    job: Job,
    *,
    suffix: str = "1",
) -> tuple[WorkspaceLease, ResourceAllocation, QuotaReservation]:
    node_id = f"node-{suffix}"
    workspace = WorkspaceLease(
        id=f"workspace-{suffix}",
        job_id=job.id,
        repository=job.repository,
        branch=f"agentd/{job.id}-{suffix}",
        working_directory=f"/workspaces/{job.id}-{suffix}",
        base_ref="HEAD",
    )
    allocation = ResourceAllocation(
        id=f"allocation-{suffix}",
        job_id=job.id,
        node_id=node_id,
        resources=job.resources,
    )
    reservation = QuotaReservation(
        id=f"reservation-{suffix}",
        job_id=job.id,
        pool_id="default",
        amount=job.quota_budget.expected_path,
    )
    store.save_node(
        WorkerNode(
            id=node_id,
            labels={},
            capacity=ResourceVector(cpu=8, ram_gb=16),
            allocated=job.resources,
            harnesses=frozenset({"fake"}),
        )
    )
    if not store.list_quota_pools():
        store.save_quota_pool(
            QuotaPool(
                id="default",
                provider="fake",
                remaining=100,
                reserved=job.quota_budget.expected_path,
            )
        )
    store.save_workspace(workspace, expected=None)
    store.save_allocation(allocation)
    store.save_reservation(reservation)
    return workspace, allocation, reservation


def test_integer_json_round_trip_does_not_cause_false_cas_conflict(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    store, node = _store_with_runtime(job)

    reservation = QuotaManager(store).reserve(job)
    allocation = ResourceManager(store).allocate(job, node)

    assert store.get_quota_pool("default").reserved == 16
    assert store.get_node(node.id).allocated == ResourceVector(cpu=2, ram_gb=4)
    assert (
        QuotaManager(store).release(reservation.id).state is ReservationState.RELEASED
    )
    assert (
        ResourceManager(store).release(allocation.id).state is AllocationState.RELEASED
    )


def test_quota_release_charges_consumption_above_reserved_amount(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _store_with_runtime(job)
    manager = QuotaManager(store)
    reservation = manager.reserve(job)

    released = manager.release(reservation.id, consumed=25)

    assert reservation.amount == 16
    assert released.consumed == 25
    assert store.get_quota_pool("default").remaining == 75
    assert store.get_quota_pool("default").reserved == 0


def test_idempotent_reserve_validates_pool_and_amount(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _store_with_runtime(job)
    store.save_quota_pool(QuotaPool(id="other", provider="fake", remaining=100))
    manager = QuotaManager(store)
    manager.reserve(job)

    wrong_amount = replace(
        job,
        quota_budget=QuotaBudget(implementation=17, pool_id="default"),
    )
    wrong_pool = replace(
        job,
        quota_budget=QuotaBudget(implementation=16, pool_id="other"),
    )

    with pytest.raises(QuotaAdmissionError, match="incompatible"):
        manager.reserve(wrong_amount)
    with pytest.raises(QuotaAdmissionError, match="incompatible"):
        manager.reserve(wrong_pool)


def test_idempotent_allocation_validates_node_and_resources(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    store, node = _store_with_runtime(job)
    other_node = replace(node, id="node-2")
    store.save_node(other_node)
    manager = ResourceManager(store)
    manager.allocate(job, node)

    with pytest.raises(ResourceAllocationError, match="incompatible"):
        manager.allocate(job, other_node)
    with pytest.raises(ResourceAllocationError, match="incompatible"):
        manager.allocate(
            replace(job, resources=ResourceVector(cpu=1, ram_gb=2)),
            node,
        )


def test_duplicate_active_reservation_rolls_back_pool_update(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _store_with_runtime(job)
    manager = QuotaManager(store)
    manager.reserve(job)
    before = store.get_quota_pool("default")
    duplicate = QuotaReservation(
        job_id=job.id,
        pool_id=before.id,
        amount=job.quota_budget.expected_path,
    )

    with pytest.raises(ConcurrentStateError, match="Could not reserve"):
        store.reserve_quota(
            before,
            replace(before, reserved=before.reserved + duplicate.amount),
            duplicate,
        )

    assert store.get_quota_pool(before.id) == before
    assert len(store.list_reservations(job.id)) == 1


def test_duplicate_active_allocation_rolls_back_node_update(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    store, node = _store_with_runtime(job)
    manager = ResourceManager(store)
    manager.allocate(job, node)
    before = store.get_node(node.id)
    duplicate = ResourceAllocation(
        job_id=job.id,
        node_id=before.id,
        resources=job.resources,
    )

    with pytest.raises(ConcurrentStateError, match="Could not create allocation"):
        store.allocate_resources(
            before,
            replace(before, allocated=before.allocated + duplicate.resources),
            duplicate,
        )

    assert store.get_node(before.id) == before
    assert len(store.list_allocations(job.id)) == 1


class ConflictOnceStore(SQLiteStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.quota_conflicts = 0
        self.resource_conflicts = 0

    def reserve_quota(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
        reservation: QuotaReservation,
    ) -> None:
        if self.quota_conflicts == 0:
            self.quota_conflicts += 1
            raise ConcurrentStateError("injected quota conflict")
        super().reserve_quota(expected_pool, updated_pool, reservation)

    def allocate_resources(
        self,
        expected_node: WorkerNode,
        updated_node: WorkerNode,
        allocation: ResourceAllocation,
    ) -> None:
        if self.resource_conflicts == 0:
            self.resource_conflicts += 1
            raise ConcurrentStateError("injected resource conflict")
        super().allocate_resources(expected_node, updated_node, allocation)


def test_managers_retry_optimistic_conflicts_without_double_accounting(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    store = ConflictOnceStore()
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    node = WorkerNode(
        id="node-1",
        labels={},
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset({"fake"}),
    )
    store.save_node(node)

    QuotaManager(store).reserve(job)
    ResourceManager(store).allocate(job, node)

    assert store.quota_conflicts == 1
    assert store.resource_conflicts == 1
    assert store.get_quota_pool("default").reserved == 16
    assert store.get_node(node.id).allocated == job.resources
    assert len(store.list_reservations(job.id)) == 1
    assert len(store.list_allocations(job.id)) == 1


def test_sqlite_rejects_forged_disallowed_job_transition(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _store_with_runtime(job)
    completed = replace(job, state=JobState.COMPLETED)
    forged = StateTransition(
        job_id=job.id,
        from_state=JobState.BACKLOG,
        to_state=JobState.COMPLETED,
        reason="skip the lifecycle",
    )

    with pytest.raises(InvalidStateTransition, match="cannot transition"):
        store.save_job(completed, forged, expected=job)

    assert store.get_job(job.id) == job
    assert len(store.list_transitions(job.id)) == 1


def test_job_and_run_finalization_is_atomic(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(state=JobState.RUNNING)
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job, reason="already running"))
    _save_run_links(store, job)
    running_run = _run(job, run_id="run-1", state=RunState.RUNNING)
    store.save_run(running_run, expected=None)

    draining, draining_event = transition_job(
        job,
        JobState.DRAINING,
        "start draining",
    )
    conflicting_run = _run(job, run_id="run-2", state=RunState.STARTING)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        store.save_job_and_run(
            draining,
            draining_event,
            conflicting_run,
            expected_job=job,
            expected_run=None,
        )

    assert store.get_job(job.id) == job
    assert store.get_run(running_run.id) == running_run
    assert len(store.list_runs(job.id)) == 1
    assert len(store.list_transitions(job.id)) == 1

    completed, completed_event = transition_job(
        job,
        JobState.COMPLETED,
        "run completed",
    )
    completed_run = replace(running_run, state=RunState.COMPLETED)
    store.save_job_and_run(
        completed,
        completed_event,
        completed_run,
        expected_job=job,
        expected_run=running_run,
    )

    assert store.get_job(job.id) == completed
    assert store.get_run(running_run.id) == completed_run
    assert [event.to_state for event in store.list_transitions(job.id)] == [
        JobState.RUNNING,
        JobState.COMPLETED,
    ]


def test_job_cas_rejects_stale_same_state_metadata_update(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(state=JobState.READY)
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job, reason="already ready"))
    selected = replace(job, selected_harness="fake")
    store.save_job(selected, expected=job)

    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.save_job(replace(job, selected_model_class="standard"), expected=job)

    assert store.get_job(job.id) == selected


def test_compound_cas_rejects_stale_run_without_advancing_job(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(state=JobState.RUNNING)
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job, reason="already running"))
    _save_run_links(store, job)
    run = _run(job, run_id="cas-run", state=RunState.RUNNING)
    store.save_run(run, expected=None)
    current_run = replace(
        run,
        handle=RunHandle(id="published-handle", driver=run.driver),
    )
    store.save_run(current_run, expected=run)
    completed, event = transition_job(job, JobState.COMPLETED, "run completed")

    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.save_job_and_run(
            completed,
            event,
            replace(run, state=RunState.COMPLETED),
            expected_job=job,
            expected_run=run,
        )

    assert store.get_job(job.id) == job
    assert store.get_run(run.id) == current_run
    assert len(store.list_transitions(job.id)) == 1

    completed_run = replace(current_run, state=RunState.COMPLETED)
    store.save_job_and_run(
        completed,
        event,
        completed_run,
        expected_job=job,
        expected_run=current_run,
    )
    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.save_run(run, expected=run)
    assert store.get_run(run.id) == completed_run


def test_workspace_and_driver_session_reject_stale_same_state_updates(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(state=JobState.RUNNING)
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job, reason="already running"))
    workspace, _allocation, _reservation = _save_run_links(store, job)
    run = _run(job, run_id="aggregate-cas-run", state=RunState.RUNNING)
    store.save_run(run, expected=None)

    current_workspace = replace(workspace, commit="current-commit")
    store.save_workspace(current_workspace, expected=workspace)
    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.save_workspace(
            replace(workspace, commit="stale-commit"),
            expected=workspace,
        )

    session = DriverSession(
        id="aggregate-cas-session",
        run_id=run.id,
        driver=run.driver,
        metadata={"generation": 0},
    )
    store.save_driver_session(session, expected=None)
    current_session = replace(session, metadata={"generation": 1})
    store.save_driver_session(current_session, expected=session)
    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.save_driver_session(
            replace(session, metadata={"generation": 2}),
            expected=session,
        )

    assert store.get_workspace(workspace.id) == current_workspace
    assert store.get_driver_session(run.id) == current_session


def test_bootstrap_and_append_only_saves_are_create_or_identical(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(state=JobState.RUNNING)
    store, node = _store_with_runtime(job)
    reservation = QuotaManager(store).reserve(job)
    allocation = ResourceManager(store).allocate(job, node)
    node = store.get_node(node.id)
    pool = store.get_quota_pool("default")
    workspace = WorkspaceLease(
        id="append-only-workspace",
        job_id=job.id,
        repository=job.repository,
        branch="agentd/append-only",
        working_directory="/workspaces/append-only",
        base_ref="HEAD",
    )
    store.save_workspace(workspace, expected=None)
    run = replace(
        _run(job, run_id="append-only-run", state=RunState.RUNNING),
        workspace_id=workspace.id,
        reservation_id=reservation.id,
        allocation_id=allocation.id,
    )
    store.save_run(run, expected=None)
    checkpoint = Checkpoint(
        id="append-only-checkpoint",
        job_id=job.id,
        run_id=run.id,
        capsule=ResumeCapsule(current=("current",)),
    )

    for save, value in (
        (store.save_node, node),
        (store.save_quota_pool, pool),
        (store.save_reservation, reservation),
        (store.save_allocation, allocation),
        (store.save_checkpoint, checkpoint),
    ):
        save(value)

    stale_values = (
        (store.save_node, replace(node, labels={"stale": "true"})),
        (store.save_quota_pool, replace(pool, provider="stale")),
        (store.save_reservation, replace(reservation, amount=99)),
        (
            store.save_allocation,
            replace(allocation, resources=ResourceVector(cpu=2, ram_gb=2)),
        ),
        (
            store.save_checkpoint,
            replace(checkpoint, capsule=ResumeCapsule(current=("stale",))),
        ),
    )
    for save, value in stale_values:
        with pytest.raises(ConcurrentStateError, match="already differs"):
            save(value)


def test_only_one_leased_workspace_per_job(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _store_with_runtime(job)
    first = WorkspaceLease(
        id="workspace-1",
        job_id=job.id,
        repository=job.repository,
        branch="agentd/job-1",
        working_directory="/workspaces/job-1",
        base_ref="HEAD",
    )
    second = replace(first, id="workspace-2", branch="agentd/job-1-second")
    store.save_workspace(first, expected=None)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        store.save_workspace(second, expected=None)

    store.save_workspace(
        replace(first, state=WorkspaceState.RELEASED),
        expected=first,
    )
    store.save_workspace(second, expected=None)
    assert store.find_workspace(job.id) == second
