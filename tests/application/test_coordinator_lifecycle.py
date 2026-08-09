from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    Job,
    QuotaPool,
    ResourceVector,
    ResumeCapsule,
    RunResult,
    WorkerNode,
    WorkspaceLease,
)
from agentd.harness import FakeHarnessCall, FakeHarnessDriver
from agentd.workspaces import WorkspaceAllocationError


def test_checkpoint_suspend_resume_complete_has_exact_durable_history(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    rig = make_application_rig()
    job = make_application_job()
    early_capsule = ResumeCapsule(
        completed=("schema",),
        current=("implementation",),
        next_steps=("checkpoint test",),
        commit="1" * 40,
        decisions=("keep orchestration outside the harness",),
    )
    resume_capsule = ResumeCapsule(
        completed=("schema", "implementation"),
        current=("checkpoint test",),
        next_steps=("validation", "review"),
        commit="2" * 40,
        known_failures=("test_timeout",),
        decisions=("resume in a new attempt",),
    )

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        assert rig.plane.submit(job).state is JobState.READY

        first_run = await rig.plane.dispatch_next()
        assert first_run is not None
        assert first_run.handle.id == "fake-run-1"
        assert first_run.contract.resume is None
        assert "commit changes" in first_run.contract.completion_protocol
        assert "Commit durable handoffs" in first_run.contract.checkpoint_expectations

        early_checkpoint = await rig.plane.checkpoint(job.id, early_capsule)
        suspended = await rig.plane.pause(job.id, resume_capsule)

        assert suspended.state is JobState.SUSPENDED
        stored_first_run = rig.store.get_run(first_run.id)
        assert stored_first_run.state is RunState.SUSPENDED
        assert stored_first_run.ended_at is not None
        assert rig.store.get_allocation(first_run.allocation_id).state is (
            AllocationState.RELEASED
        )
        assert rig.store.get_reservation(first_run.reservation_id).state is (
            ReservationState.RELEASED
        )
        assert rig.store.get_node(application_node.id).allocated == ResourceVector(0, 0)
        assert rig.store.get_quota_pool("default").reserved == 0
        assert rig.plane.inspect_workspace(job.id).state is WorkspaceState.LEASED  # type: ignore[union-attr]

        checkpoints = rig.plane.checkpoints(job.id)
        assert len(checkpoints) == 2
        assert checkpoints[0] == early_checkpoint
        assert checkpoints[0].capsule == early_capsule
        assert checkpoints[1].run_id == first_run.id
        assert checkpoints[1].capsule == resume_capsule

        assert (await rig.plane.resume(job.id)).state is JobState.READY
        second_run = await rig.plane.dispatch_next()
        assert second_run is not None
        assert second_run.id != first_run.id
        assert second_run.handle.id == "fake-run-2"
        assert second_run.workspace_id == first_run.workspace_id
        assert second_run.contract.resume == resume_capsule
        assert rig.workspaces.allocations == [(job.id, "HEAD")]

        completed = await rig.plane.complete(job.id)
        assert completed.state is JobState.COMPLETED

        runs = rig.plane.runs(job.id)
        assert [run.state for run in runs] == [
            RunState.SUSPENDED,
            RunState.COMPLETED,
        ]
        assert runs[1].result is not None
        assert runs[1].result.summary == "accepted artifact"
        assert all(
            allocation.state is AllocationState.RELEASED
            for allocation in rig.store.list_allocations(job.id)
        )
        reservations = rig.store.list_reservations(job.id)
        assert [reservation.state for reservation in reservations] == [
            ReservationState.RELEASED,
            ReservationState.RELEASED,
        ]
        assert [reservation.consumed for reservation in reservations] == [7, 7]
        pool = rig.plane.inspect_quota("default")
        assert pool.remaining == 86
        assert pool.reserved == 0
        workspace = rig.plane.inspect_workspace(job.id)
        assert workspace is not None
        assert workspace.state is WorkspaceState.RELEASED
        assert workspace.commit == "f" * 40
        assert rig.workspaces.releases == [first_run.workspace_id]

        assert isinstance(rig.driver, FakeHarnessDriver)
        assert rig.driver.calls_for(first_run.handle) == (
            FakeHarnessCall("start"),
            FakeHarnessCall(
                "steer",
                "Stop at the next safe boundary and preserve the supplied "
                "resume state.",
            ),
            FakeHarnessCall("interrupt"),
            FakeHarnessCall("collect"),
        )
        assert rig.driver.calls_for(second_run.handle) == (
            FakeHarnessCall("start"),
            FakeHarnessCall("collect"),
        )

        history = rig.plane.history(job.id)
        assert [(item.from_state, item.to_state) for item in history] == [
            (None, JobState.BACKLOG),
            (JobState.BACKLOG, JobState.READY),
            (JobState.READY, JobState.ADMITTED),
            (JobState.ADMITTED, JobState.RUNNING),
            (JobState.RUNNING, JobState.DRAINING),
            (JobState.DRAINING, JobState.CHECKPOINTED),
            (JobState.CHECKPOINTED, JobState.SUSPENDED),
            (JobState.SUSPENDED, JobState.READY),
            (JobState.READY, JobState.ADMITTED),
            (JobState.ADMITTED, JobState.RUNNING),
            (JobState.RUNNING, JobState.COMPLETED),
        ]
        assert history[0].reason == "job submitted"
        assert history[1].reason == "job accepted into the ready queue"
        assert history[2].reason == "admitted on node node-1 with fake"
        assert history[3].reason == f"harness run {first_run.id} started"
        assert history[4].reason == (
            "suspension requested; draining to a safe checkpoint"
        )
        assert history[5].reason == (f"durable checkpoint {checkpoints[1].id} recorded")
        assert history[6].reason == (
            "checkpoint durable; scarce execution resources released"
        )
        assert history[7].reason == ("resume requested; queued for a new run attempt")
        assert history[8].reason == "admitted on node node-1 with fake"
        assert history[9].reason == f"harness run {second_run.id} started"
        assert history[10].reason == (f"run {second_run.id} reported completed")
        assert rig.store.find_active_run(job.id) is None

    asyncio.run(scenario())


def test_turn_boundary_driver_finishes_steered_turn_before_suspension(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    driver = FakeHarnessDriver(
        capabilities=HarnessCapabilities(
            name="fake",
            models=frozenset({"standard"}),
            features=frozenset({"checkpointing", "steering"}),
            native_pause=False,
        ),
        result=RunResult(
            outcome=RunOutcome.COMPLETED,
            summary="safe boundary reached",
            commit="b" * 40,
            consumed_quota=2,
        ),
        id_factory=lambda: "turn-boundary-run",
    )
    rig = make_application_rig(driver=driver)
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)
        run = await rig.plane.dispatch_next()
        assert run is not None

        suspended = await rig.plane.pause(
            job.id,
            ResumeCapsule(current=("safe-boundary handoff",)),
        )

        assert suspended.state is JobState.SUSPENDED
        assert driver.calls_for(run.handle) == (
            FakeHarnessCall("start"),
            FakeHarnessCall(
                "steer",
                "Stop at the next safe boundary and preserve the supplied "
                "resume state.",
            ),
            FakeHarnessCall("collect"),
        )
        assert rig.store.get_run(run.id).result is not None

    asyncio.run(scenario())


class FailingStartDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "unused")
        self.contracts: list[ExecutionContract] = []

    async def start(self, execution: ExecutionContract):
        self.contracts.append(execution)
        raise RuntimeError("injected driver start failure")


class SelectivelyFailingWorkspaceManager:
    def __init__(self, failing_job_id: str) -> None:
        self._failing_job_id = failing_job_id
        self._leases: dict[str, WorkspaceLease] = {}

    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        if job.id == self._failing_job_id:
            raise WorkspaceAllocationError("injected bad repository")
        lease = WorkspaceLease(
            id=f"workspace-{job.id}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
        )
        self._leases[lease.id] = lease
        return lease

    def is_available(self, lease: WorkspaceLease) -> bool:
        return self._leases.get(lease.id) == lease

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        released = replace(lease, state=WorkspaceState.RELEASED)
        self._leases[lease.id] = released
        return released

    def current_commit(self, _lease: WorkspaceLease) -> str:
        return "f" * 40

    def commit_changes(self, lease: WorkspaceLease) -> str:
        return self.current_commit(lease)


def test_driver_start_failure_compensates_admission_effects(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    driver = FailingStartDriver()
    rig = make_application_rig(driver=driver)
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)

        with pytest.raises(RuntimeError, match="driver start failure"):
            await rig.plane.dispatch_next()

        assert rig.plane.inspect_job(job.id).state is JobState.READY
        assert [item.to_state for item in rig.plane.history(job.id)] == [
            JobState.BACKLOG,
            JobState.READY,
            JobState.ADMITTED,
            JobState.READY,
        ]
        assert [item.reason for item in rig.plane.history(job.id)][-1] == (
            "dispatch failed; admission resources compensated"
        )
        assert len(driver.contracts) == 1
        failed_runs = rig.plane.runs(job.id)
        assert len(failed_runs) == 1
        assert failed_runs[0].state is RunState.CANCELLED
        assert failed_runs[0].result is not None
        assert failed_runs[0].result.summary == (
            "dispatch failed before the run became active"
        )
        assert rig.store.get_node(application_node.id).allocated == ResourceVector(0, 0)
        allocations = rig.store.list_allocations(job.id)
        assert len(allocations) == 1
        assert allocations[0].state is AllocationState.RELEASED
        reservations = rig.store.list_reservations(job.id)
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.CANCELLED
        assert rig.store.get_quota_pool("default").reserved == 0
        workspace = rig.plane.inspect_workspace(job.id)
        assert workspace is not None
        assert workspace.state is WorkspaceState.RELEASED
        assert rig.workspaces.allocations == [(job.id, "HEAD")]
        assert rig.workspaces.releases == [workspace.id]

    asyncio.run(scenario())


def test_bad_high_priority_job_does_not_starve_runnable_peer(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    bad = make_application_job(id="bad", priority=100)
    good = make_application_job(id="good", priority=0)
    workspaces = SelectivelyFailingWorkspaceManager(bad.id)
    rig = make_application_rig(workspaces=workspaces)

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(bad)
        rig.plane.submit(good)

        run = await rig.plane.dispatch_next()

        assert run is not None
        assert run.job_id == good.id
        assert rig.plane.inspect_job(bad.id).state is JobState.READY
        assert rig.store.find_active_allocation(bad.id) is None
        assert rig.store.find_active_reservation(bad.id) is None

    asyncio.run(scenario())


def test_quota_denial_has_no_partial_admission_effects(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    rig = make_application_rig()
    job = make_application_job()
    insufficient_pool = replace(
        application_quota_pool,
        remaining=9,
        minimum_interactive_reserve=0,
    )

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(insufficient_pool)
        rig.plane.submit(job)

        assert await rig.plane.dispatch_next() is None
        assert rig.plane.inspect_job(job.id).state is JobState.READY
        assert [item.to_state for item in rig.plane.history(job.id)] == [
            JobState.BACKLOG,
            JobState.READY,
        ]
        assert rig.store.list_reservations(job.id) == []
        assert rig.store.list_allocations(job.id) == []
        assert rig.store.list_workspaces(job.id) == []
        assert rig.plane.runs(job.id) == []
        assert rig.workspaces.allocations == []
        assert rig.store.get_node(application_node.id) == application_node
        assert rig.store.get_quota_pool("default") == insufficient_pool

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_by", ["dependency", "placement"])
def test_dependency_or_placement_block_has_no_admission_effects(
    blocked_by: str,
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    rig = make_application_rig()
    if blocked_by == "dependency":
        job = make_application_job(dependencies=("missing-dependency",))
        node = application_node
    else:
        job = make_application_job()
        node = replace(application_node, harnesses=frozenset({"codex"}))

    async def scenario() -> None:
        rig.plane.register_node(node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)

        assert await rig.plane.dispatch_next() is None
        assert rig.plane.inspect_job(job.id).state is JobState.READY
        assert rig.store.list_reservations(job.id) == []
        assert rig.store.list_allocations(job.id) == []
        assert rig.store.list_workspaces(job.id) == []
        assert rig.plane.runs(job.id) == []
        assert rig.workspaces.allocations == []
        assert rig.store.get_quota_pool("default") == application_quota_pool

    asyncio.run(scenario())
