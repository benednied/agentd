from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import JobState, RunOutcome, RunState, WorkspaceState
from agentd.domain.models import (
    Job,
    QuotaPool,
    ResourceVector,
    RunHandle,
    RunResult,
    WorkerNode,
    WorkspaceLease,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

FIXED_TIME = datetime(2026, 3, 1, tzinfo=UTC)
INVALID_CONSUMPTION = pytest.mark.parametrize(
    "consumed_quota",
    [-1.0, float("nan"), float("inf"), float("-inf")],
    ids=["negative", "nan", "positive-infinity", "negative-infinity"],
)


@INVALID_CONSUMPTION
def test_run_result_rejects_invalid_consumed_quota(consumed_quota: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        RunResult(
            outcome=RunOutcome.COMPLETED,
            consumed_quota=consumed_quota,
        )

    with pytest.raises(ValueError, match="finite and non-negative"):
        RunResult.from_dict(
            {
                "outcome": RunOutcome.COMPLETED.value,
                "consumed_quota": consumed_quota,
            }
        )


class InvalidQuotaDriver(FakeHarnessDriver):
    def __init__(self, consumed_quota: float) -> None:
        super().__init__(id_factory=lambda: "invalid-quota-run")
        self._consumed_quota = consumed_quota

    async def collect(self, run: RunHandle) -> RunResult:
        await super().collect(run)
        return RunResult(
            outcome=RunOutcome.COMPLETED,
            summary="invalid driver usage",
            commit="a" * 40,
            consumed_quota=self._consumed_quota,
        )


class UnitWorkspaceManager:
    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        return WorkspaceLease(
            id="unit-workspace",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
            created_at=FIXED_TIME,
        )

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        return replace(
            lease,
            state=WorkspaceState.RELEASED,
            released_at=FIXED_TIME,
        )

    def is_available(self, lease: WorkspaceLease) -> bool:
        return lease.state is WorkspaceState.LEASED

    def current_commit(self, lease: WorkspaceLease) -> str:
        return lease.commit or "b" * 40


@INVALID_CONSUMPTION
def test_invalid_driver_result_cannot_persist_or_terminalize_run(
    consumed_quota: float,
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    driver = InvalidQuotaDriver(consumed_quota)
    plane = ControlPlane(
        store,
        coordinator=SchedulerCoordinator(
            store,
            UnitWorkspaceManager(),
            DriverRegistry((driver,)),
        ),
    )
    job = make_job(id="invalid-driver-job")

    async def scenario() -> None:
        plane.register_node(
            WorkerNode(
                id="unit-node",
                labels={},
                capacity=ResourceVector(cpu=4, ram_gb=8),
                harnesses=frozenset({"fake"}),
                updated_at=FIXED_TIME,
            )
        )
        plane.register_quota_pool(
            QuotaPool(
                id="default",
                provider="unit-provider",
                remaining=100,
                updated_at=FIXED_TIME,
            )
        )
        plane.submit(job)
        run = await plane.dispatch_next()
        assert run is not None

        with pytest.raises(ValueError, match="finite and non-negative"):
            await plane.complete(job.id)

        assert plane.inspect_job(job.id).state is JobState.RUNNING
        stored_run = store.get_run(run.id)
        assert stored_run.state is RunState.RUNNING
        assert stored_run.result is None
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
        assert JobState.COMPLETED not in {
            transition.to_state for transition in plane.history(job.id)
        }

    try:
        asyncio.run(scenario())
    finally:
        store.close()
