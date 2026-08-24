from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import AllocationState, JobState, ReservationState, RunState
from agentd.domain.models import (
    Checkpoint,
    ExecutionContract,
    Job,
    QuotaPool,
    ResourceVector,
    ResumeCapsule,
    RunHandle,
    RunRecord,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import initial_transition
from agentd.runtime.quota import QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.state.sqlite import SQLiteStateStore


def _contract(job: Job) -> ExecutionContract:
    return ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="ownership test",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=(f"/workspaces/{job.id}",),
        checkpoint_expectations="checkpoint safely",
        coordination_mechanisms=(),
        completion_protocol="validate and report",
        working_directory=f"/workspaces/{job.id}",
        environment={},
        model_class="standard",
    )


def _runtime(
    make_job: Callable[..., Job],
) -> tuple[
    SQLiteStateStore,
    Job,
    Job,
    WorkerNode,
    dict[str, object],
    dict[str, object],
]:
    store = SQLiteStateStore()
    job_a = make_job(id="job-a", state=JobState.RUNNING)
    job_b = make_job(id="job-b", state=JobState.RUNNING)
    for job in (job_a, job_b):
        store.create_job(job, initial_transition(job, reason="running test job"))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    node = WorkerNode(
        id="node",
        labels={},
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset({"fake"}),
    )
    store.save_node(node)

    links: list[dict[str, object]] = []
    quota = QuotaManager(store)
    resources = ResourceManager(store)
    for job in (job_a, job_b):
        workspace = WorkspaceLease(
            id=f"workspace-{job.id}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref="HEAD",
        )
        store.save_workspace(workspace, expected=None)
        links.append(
            {
                "workspace": workspace,
                "allocation": resources.allocate(job, node),
                "reservation": quota.reserve(job),
            }
        )
    return store, job_a, job_b, node, links[0], links[1]


def _run(job: Job, links: dict[str, object], *, run_id: str) -> RunRecord:
    workspace = links["workspace"]
    allocation = links["allocation"]
    reservation = links["reservation"]
    assert isinstance(workspace, WorkspaceLease)
    from agentd.domain.models import QuotaReservation, ResourceAllocation

    assert isinstance(allocation, ResourceAllocation)
    assert isinstance(reservation, QuotaReservation)
    return RunRecord(
        id=run_id,
        job_id=job.id,
        node_id=allocation.node_id,
        workspace_id=workspace.id,
        reservation_id=reservation.id,
        allocation_id=allocation.id,
        driver="fake",
        backend="local",
        contract=_contract(job),
        handle=RunHandle(id=f"handle-{run_id}", driver="fake"),
        state=RunState.RUNNING,
    )


def test_run_persistence_rejects_cross_job_runtime_links(
    make_job: Callable[..., Job],
) -> None:
    store, job_a, job_b, _node, links_a, links_b = _runtime(make_job)
    valid = _run(job_a, links_a, run_id="run-a")
    foreign_workspace = links_b["workspace"]
    foreign_allocation = links_b["allocation"]
    foreign_reservation = links_b["reservation"]
    assert isinstance(foreign_workspace, WorkspaceLease)
    from agentd.domain.models import QuotaReservation, ResourceAllocation

    assert isinstance(foreign_allocation, ResourceAllocation)
    assert isinstance(foreign_reservation, QuotaReservation)

    invalid_runs = (
        replace(valid, workspace_id=foreign_workspace.id),
        replace(valid, allocation_id=foreign_allocation.id),
        replace(valid, reservation_id=foreign_reservation.id),
        replace(valid, node_id="another-node"),
        replace(valid, contract=_contract(job_b)),
        replace(valid, handle=RunHandle(id="wrong-driver", driver="codex")),
    )
    for invalid in invalid_runs:
        with pytest.raises(ValueError):
            store.save_run(invalid, expected=None)

    assert store.list_runs(job_a.id) == []
    assert store.get_allocation(foreign_allocation.id).state is AllocationState.ACTIVE
    assert store.get_reservation(foreign_reservation.id).state is (
        ReservationState.ACTIVE
    )

    store.save_run(valid, expected=None)
    assert store.get_run(valid.id) == valid


def test_checkpoint_must_belong_to_its_runs_job(
    make_job: Callable[..., Job],
) -> None:
    store, job_a, job_b, _node, links_a, links_b = _runtime(make_job)
    run_a = _run(job_a, links_a, run_id="run-a")
    run_b = _run(job_b, links_b, run_id="run-b")
    store.save_run(run_a, expected=None)
    store.save_run(run_b, expected=None)
    checkpoint = Checkpoint(
        id="checkpoint",
        job_id=job_a.id,
        run_id=run_a.id,
        capsule=ResumeCapsule(current=("testing",)),
    )

    with pytest.raises(ValueError, match="another run's job"):
        store.save_checkpoint(replace(checkpoint, job_id=job_b.id))
    store.save_checkpoint(checkpoint)
    with pytest.raises(ValueError, match="cannot change ownership"):
        store.save_checkpoint(replace(checkpoint, job_id=job_b.id, run_id=run_b.id))

    assert store.list_checkpoints(job_a.id) == [checkpoint]
    assert store.list_checkpoints(job_b.id) == []


def test_generic_upserts_cannot_reassign_job_ownership(
    make_job: Callable[..., Job],
) -> None:
    store, job_a, job_b, _node, links_a, links_b = _runtime(make_job)
    workspace = links_a["workspace"]
    allocation = links_a["allocation"]
    reservation = links_a["reservation"]
    assert isinstance(workspace, WorkspaceLease)
    from agentd.domain.models import QuotaReservation, ResourceAllocation

    assert isinstance(allocation, ResourceAllocation)
    assert isinstance(reservation, QuotaReservation)

    with pytest.raises(ValueError, match="cannot change ownership"):
        store.save_workspace(
            replace(workspace, job_id=job_b.id),
            expected=workspace,
        )
    with pytest.raises(ValueError, match="cannot change ownership"):
        store.save_allocation(replace(allocation, job_id=job_b.id))
    with pytest.raises(ValueError, match="cannot change ownership"):
        store.save_reservation(replace(reservation, job_id=job_b.id))

    run = _run(job_a, links_a, run_id="run")
    store.save_run(run, expected=None)
    reassigned = _run(job_b, links_b, run_id=run.id)
    with pytest.raises(ValueError, match="cannot change ownership"):
        store.save_run(reassigned, expected=run)

    assert store.get_workspace(workspace.id).job_id == job_a.id
    assert store.get_allocation(allocation.id).job_id == job_a.id
    assert store.get_reservation(reservation.id).job_id == job_a.id
    assert store.get_run(run.id).job_id == job_a.id
