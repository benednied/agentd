from __future__ import annotations

import asyncio
from collections.abc import Callable

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
    Job,
    QuotaPool,
    ResourceVector,
    ResumeCapsule,
    RunHandle,
    RunResult,
    WorkerNode,
)
from agentd.harness import FakeHarnessCall, FakeHarnessDriver
from agentd.runtime.quota import QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.state.sqlite import SQLiteStateStore


class CollectObservingDriver(FakeHarnessDriver):
    def __init__(self, store: SQLiteStateStore, result: RunResult) -> None:
        super().__init__(result=result, id_factory=lambda: "observed-run")
        self._store = store
        self.observed_before_collect: list[
            tuple[AllocationState, ReservationState]
        ] = []

    async def collect(self, run: RunHandle) -> RunResult:
        record = next(
            candidate
            for candidate in self._store.list_runs()
            if candidate.handle == run
        )
        self.observed_before_collect.append(
            (
                self._store.get_allocation(record.allocation_id).state,
                self._store.get_reservation(record.reservation_id).state,
            )
        )
        return await super().collect(run)


@pytest.mark.parametrize("lifecycle", ["review", "suspend"])
def test_review_or_suspend_collects_and_persists_result_before_release(
    lifecycle: str,
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        summary=f"{lifecycle} boundary",
        commit="d" * 40,
        consumed_quota=4,
        metadata={"input_tokens": 21, "output_tokens": 5},
    )
    rig = make_regression_rig(
        driver_factory=lambda store: CollectObservingDriver(store, result)
    )
    job = make_regression_job()

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(job)
        run = await rig.plane.dispatch_next()
        assert run is not None

        if lifecycle == "review":
            final_job = await rig.plane.request_review(job.id)
            expected_job_state = JobState.REVIEW
            expected_calls = (
                FakeHarnessCall("start"),
                FakeHarnessCall("interrupt"),
                FakeHarnessCall("collect"),
            )
        else:
            final_job = await rig.plane.pause(
                job.id,
                ResumeCapsule(
                    completed=("implementation",),
                    current=("validation",),
                    commit=result.commit,
                ),
            )
            expected_job_state = JobState.SUSPENDED
            expected_calls = (
                FakeHarnessCall("start"),
                FakeHarnessCall(
                    "steer",
                    "Stop at the next safe boundary and preserve the supplied "
                    "resume state.",
                ),
                FakeHarnessCall("interrupt"),
                FakeHarnessCall("collect"),
            )

        assert final_job.state is expected_job_state
        stored_run = rig.store.get_run(run.id)
        assert stored_run.state is RunState.SUSPENDED
        assert stored_run.result == result
        assert stored_run.ended_at is not None
        assert rig.store.get_allocation(run.allocation_id).state is (
            AllocationState.RELEASED
        )
        reservation = rig.store.get_reservation(run.reservation_id)
        assert reservation.state is ReservationState.RELEASED
        assert reservation.consumed == result.consumed_quota
        assert rig.plane.inspect_quota("default").remaining == 96
        assert rig.plane.inspect_quota("default").reserved == 0
        assert isinstance(rig.driver, CollectObservingDriver)
        assert rig.driver.observed_before_collect == [
            (AllocationState.ACTIVE, ReservationState.ACTIVE)
        ]
        assert rig.driver.calls_for(run.handle) == expected_calls
        workspace = rig.plane.inspect_workspace(job.id)
        assert workspace is not None
        assert workspace.state is WorkspaceState.LEASED

    asyncio.run(scenario())


def test_review_completion_hands_dependency_the_reviewed_commit(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    commit = "e" * 40
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        summary="reviewed artifact",
        commit=commit,
        consumed_quota=3,
    )
    rig = make_regression_rig(result=result)
    producer = make_regression_job(id="producer")
    consumer = make_regression_job(id="consumer", dependencies=(producer.id,))

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(producer)
        producer_run = await rig.plane.dispatch_next()
        assert producer_run is not None
        assert producer_run.job_id == producer.id
        assert (await rig.plane.request_review(producer.id)).state is JobState.REVIEW
        assert (await rig.plane.complete(producer.id)).state is JobState.COMPLETED

        rig.plane.submit(consumer)
        consumer_run = await rig.plane.dispatch_next()

        assert consumer_run is not None
        assert consumer_run.job_id == consumer.id
        assert consumer_run.contract.dependency_results == {producer.id: commit}
        assert rig.workspaces.allocations == [
            (producer.id, "HEAD"),
            (consumer.id, commit),
        ]

    asyncio.run(scenario())


class FailOnceFinalJobSaveStore(SQLiteStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def save_job(self, job, transition=None) -> None:
        if (
            not self.failed
            and transition is not None
            and job.state is JobState.COMPLETED
        ):
            self.failed = True
            raise RuntimeError("injected final job save failure")
        super().save_job(job, transition)

    def save_job_and_run(self, job, transition, run) -> None:
        if not self.failed and job.state is JobState.COMPLETED:
            self.failed = True
            raise RuntimeError("injected final job save failure")
        super().save_job_and_run(job, transition, run)


def test_completion_can_be_retried_after_final_job_persistence_failure(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    store = FailOnceFinalJobSaveStore()
    result = RunResult(
        outcome=RunOutcome.COMPLETED,
        summary="retry-safe artifact",
        commit="a" * 40,
        consumed_quota=4,
    )
    rig = make_regression_rig(store=store, result=result)
    job = make_regression_job()

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(job)
        run = await rig.plane.dispatch_next()
        assert run is not None

        with pytest.raises(RuntimeError, match="final job save failure"):
            await rig.plane.complete(job.id)

        assert rig.store.get_job(job.id).state is JobState.RUNNING
        assert rig.store.get_run(run.id).state is RunState.RUNNING
        assert JobState.COMPLETED not in {
            transition.to_state for transition in rig.plane.history(job.id)
        }

        completed = await rig.plane.complete(job.id)

        assert completed.state is JobState.COMPLETED
        assert rig.store.get_job(job.id).state is JobState.COMPLETED
        stored_run = rig.store.get_run(run.id)
        assert stored_run.state is RunState.COMPLETED
        assert stored_run.result == result
        assert rig.store.get_allocation(run.allocation_id).state is (
            AllocationState.RELEASED
        )
        reservation = rig.store.get_reservation(run.reservation_id)
        assert reservation.state is ReservationState.RELEASED
        assert reservation.consumed == 4
        assert rig.plane.inspect_quota("default").remaining == 96
        assert rig.workspaces.release_effects == [run.workspace_id]
        assert [item.to_state for item in rig.plane.history(job.id)].count(
            JobState.COMPLETED
        ) == 1

    asyncio.run(scenario())


def test_cancel_cleans_active_job_resources_even_without_run(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    rig = make_regression_rig()
    job = make_regression_job()

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        ready = rig.plane.submit(job)
        reservation = QuotaManager(rig.store).reserve(ready)
        allocation = ResourceManager(rig.store).allocate(ready, regression_node)
        workspace = rig.workspaces.allocate(ready)
        rig.store.save_workspace(workspace)
        assert rig.plane.runs(job.id) == []

        cancelled = await rig.plane.cancel(job.id)

        assert cancelled.state is JobState.CANCELLED
        assert rig.store.get_allocation(allocation.id).state is (
            AllocationState.RELEASED
        )
        stored_reservation = rig.store.get_reservation(reservation.id)
        assert stored_reservation.state is ReservationState.CANCELLED
        assert rig.store.get_node(regression_node.id).allocated == ResourceVector(0, 0)
        assert rig.plane.inspect_quota("default").reserved == 0
        stored_workspace = rig.plane.inspect_workspace(job.id)
        assert stored_workspace is not None
        assert stored_workspace.state is WorkspaceState.RELEASED
        assert rig.workspaces.release_effects == [workspace.id]

    asyncio.run(scenario())
