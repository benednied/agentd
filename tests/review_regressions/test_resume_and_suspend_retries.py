from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import Job, QuotaPool, ResumeCapsule, RunHandle, WorkerNode
from agentd.harness import FakeHarnessCall, FakeHarnessDriver


class FailFirstInterruptDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "phase-retry-run")
        self.interrupt_attempts = 0

    async def interrupt(self, run: RunHandle) -> None:
        self.interrupt_attempts += 1
        if self.interrupt_attempts == 1:
            raise RuntimeError("injected interrupt failure")
        await super().interrupt(run)


def test_suspend_retries_from_durable_checkpoint_phase_without_duplication(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    driver = FailFirstInterruptDriver()
    rig = make_regression_rig(driver_factory=lambda _store: driver)
    job = make_regression_job()
    capsule = ResumeCapsule(
        completed=("implementation",),
        current=("validation",),
        next_steps=("request review",),
        commit="8" * 40,
    )

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(job)
        run = await rig.plane.dispatch_next()
        assert run is not None

        with pytest.raises(RuntimeError, match="interrupt failure"):
            await rig.plane.pause(job.id, capsule)

        assert rig.plane.inspect_job(job.id).state is JobState.CHECKPOINTED
        assert rig.store.get_run(run.id).state is RunState.CHECKPOINTED
        assert rig.store.get_allocation(run.allocation_id).state is (
            AllocationState.ACTIVE
        )
        assert rig.store.get_reservation(run.reservation_id).state is (
            ReservationState.ACTIVE
        )
        checkpoints = rig.plane.checkpoints(job.id)
        assert len(checkpoints) == 1
        assert checkpoints[0].capsule == capsule
        assert driver.interrupt_attempts == 1

        suspended = await rig.plane.pause(job.id, capsule)

        assert suspended.state is JobState.SUSPENDED
        stored_run = rig.store.get_run(run.id)
        assert stored_run.state is RunState.SUSPENDED
        assert stored_run.result is not None
        assert rig.store.get_allocation(run.allocation_id).state is (
            AllocationState.RELEASED
        )
        reservation = rig.store.get_reservation(run.reservation_id)
        assert reservation.state is ReservationState.RELEASED
        assert reservation.consumed == stored_run.result.consumed_quota
        assert rig.plane.checkpoints(job.id) == checkpoints
        assert driver.interrupt_attempts == 2
        assert driver.calls_for(run.handle) == (
            FakeHarnessCall("start"),
            FakeHarnessCall(
                "steer",
                "Stop at the next safe boundary and preserve the supplied "
                "resume state.",
            ),
            FakeHarnessCall("interrupt"),
            FakeHarnessCall("collect"),
        )
        history = rig.plane.history(job.id)
        assert [transition.to_state for transition in history].count(
            JobState.DRAINING
        ) == 1
        assert [transition.to_state for transition in history].count(
            JobState.CHECKPOINTED
        ) == 1
        assert [transition.to_state for transition in history].count(
            JobState.SUSPENDED
        ) == 1

    asyncio.run(scenario())


def test_resume_recreates_missing_workspace_from_checkpoint_commit(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    rig = make_regression_rig()
    job = make_regression_job()
    checkpoint_commit = "9" * 40
    capsule = ResumeCapsule(
        completed=("implementation",),
        current=("validation",),
        commit=checkpoint_commit,
    )

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(job)
        first_run = await rig.plane.dispatch_next()
        assert first_run is not None
        assert (await rig.plane.pause(job.id, capsule)).state is JobState.SUSPENDED

        rig.workspaces.unavailable.add(first_run.workspace_id)
        assert (await rig.plane.resume(job.id)).state is JobState.READY
        second_run = await rig.plane.dispatch_next()

        assert second_run is not None
        assert second_run.id != first_run.id
        assert second_run.workspace_id != first_run.workspace_id
        assert second_run.contract.resume == capsule
        assert rig.workspaces.availability_checks == [first_run.workspace_id]
        assert rig.workspaces.allocations == [
            (job.id, "HEAD"),
            (job.id, checkpoint_commit),
        ]
        workspaces = rig.store.list_workspaces(job.id)
        assert [workspace.state for workspace in workspaces] == [
            WorkspaceState.FAILED,
            WorkspaceState.LEASED,
        ]
        assert workspaces[1].base_ref == checkpoint_commit

        assert (await rig.plane.complete(job.id)).state is JobState.COMPLETED
        assert rig.store.get_workspace(second_run.workspace_id).state is (
            WorkspaceState.RELEASED
        )

    asyncio.run(scenario())
