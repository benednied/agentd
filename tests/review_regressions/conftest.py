from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import RunOutcome, WorkspaceState
from agentd.domain.models import (
    EffortEstimate,
    Job,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    RunResult,
    WorkerNode,
    WorkspaceLease,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver, HarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

FIXED_TIME = datetime(2026, 2, 1, tzinfo=UTC)


@dataclass(slots=True)
class RegressionWorkspaceManager:
    allocations: list[tuple[str, str]] = field(default_factory=list)
    release_calls: list[str] = field(default_factory=list)
    release_effects: list[str] = field(default_factory=list)
    availability_checks: list[str] = field(default_factory=list)
    unavailable: set[str] = field(default_factory=set)
    leases: dict[str, WorkspaceLease] = field(default_factory=dict)

    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        self.allocations.append((job.id, base_ref))
        lease = WorkspaceLease(
            id=f"workspace-{len(self.allocations)}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
            environment={"AGENTD_JOB_ID": job.id},
            created_at=FIXED_TIME + timedelta(microseconds=len(self.allocations)),
        )
        self.leases[lease.id] = lease
        return lease

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        self.release_calls.append(lease.id)
        if lease.state is WorkspaceState.RELEASED:
            return lease
        self.release_effects.append(lease.id)
        released = replace(
            lease,
            commit=lease.commit or "f" * 40,
            state=WorkspaceState.RELEASED,
            released_at=FIXED_TIME,
        )
        self.leases[lease.id] = released
        return released

    def current_commit(self, lease: WorkspaceLease) -> str:
        return lease.commit or "f" * 40

    def commit_changes(self, lease: WorkspaceLease) -> str:
        return self.current_commit(lease)

    def is_available(self, lease: WorkspaceLease) -> bool:
        self.availability_checks.append(lease.id)
        return (
            lease.state is WorkspaceState.LEASED
            and lease.id not in self.unavailable
            and self.leases.get(lease.id) == lease
        )


@dataclass(slots=True)
class RegressionRig:
    store: SQLiteStateStore
    workspaces: RegressionWorkspaceManager
    driver: HarnessDriver
    plane: ControlPlane


DriverFactory = Callable[[SQLiteStateStore], HarnessDriver]


@pytest.fixture
def make_regression_rig() -> Iterator[Callable[..., RegressionRig]]:
    stores: list[SQLiteStateStore] = []

    def factory(
        *,
        store: SQLiteStateStore | None = None,
        driver_factory: DriverFactory | None = None,
        result: RunResult | None = None,
    ) -> RegressionRig:
        state = store or SQLiteStateStore()
        stores.append(state)
        workspaces = RegressionWorkspaceManager()
        run_numbers = count(1)
        driver = (
            driver_factory(state)
            if driver_factory is not None
            else FakeHarnessDriver(
                result=result
                or RunResult(
                    outcome=RunOutcome.COMPLETED,
                    summary="accepted artifact",
                    commit="c" * 40,
                    consumed_quota=4,
                    metadata={"input_tokens": 12, "output_tokens": 3},
                ),
                id_factory=lambda: f"fake-run-{next(run_numbers)}",
            )
        )
        coordinator = SchedulerCoordinator(
            state,
            workspaces,
            DriverRegistry((driver,)),
        )
        return RegressionRig(
            store=state,
            workspaces=workspaces,
            driver=driver,
            plane=ControlPlane(state, coordinator=coordinator),
        )

    yield factory

    for store in stores:
        store.close()


@pytest.fixture
def make_regression_job() -> Callable[..., Job]:
    def factory(**overrides: object) -> Job:
        values: dict[str, object] = {
            "id": "job-1",
            "project": "review-regressions",
            "repository": "/repositories/review-regressions",
            "objective": "exercise a review regression",
            "quota_budget": QuotaBudget(
                implementation=6,
                review=1,
                repair=2,
                validation=1,
                maximum=20,
            ),
            "effort": EffortEstimate(p50=3, p90=6, p99=12),
            "resources": ResourceVector(cpu=2, ram_gb=4),
            "acceptance_criteria": ("the regression stays fixed",),
            "created_at": FIXED_TIME,
            "updated_at": FIXED_TIME,
        }
        values.update(overrides)
        return Job(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def regression_node() -> WorkerNode:
    return WorkerNode(
        id="node-1",
        labels={"os": "linux", "arch": "x86_64"},
        capacity=ResourceVector(cpu=8, ram_gb=32),
        harnesses=frozenset({"fake"}),
        updated_at=FIXED_TIME,
    )


@pytest.fixture
def regression_pool() -> QuotaPool:
    return QuotaPool(
        id="default",
        provider="fake-provider",
        remaining=100,
        minimum_interactive_reserve=5,
        updated_at=FIXED_TIME,
    )
