from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class FakeWorkspaceManager:
    allocations: list[tuple[str, str]] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
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
            created_at=FIXED_TIME,
        )
        self.leases[lease.id] = lease
        return lease

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        self.releases.append(lease.id)
        released = replace(
            lease,
            commit=lease.commit or "f" * 40,
            state=WorkspaceState.RELEASED,
            released_at=FIXED_TIME,
        )
        self.leases[lease.id] = released
        return released

    def is_available(self, lease: WorkspaceLease) -> bool:
        return (
            lease.state is WorkspaceState.LEASED and self.leases.get(lease.id) == lease
        )

    def current_commit(self, lease: WorkspaceLease) -> str:
        return lease.commit or "f" * 40


@dataclass(slots=True)
class ApplicationRig:
    store: SQLiteStateStore
    workspaces: FakeWorkspaceManager
    driver: HarnessDriver
    plane: ControlPlane


@pytest.fixture
def make_application_rig() -> Iterator[Callable[..., ApplicationRig]]:
    stores: list[SQLiteStateStore] = []

    def factory(
        *,
        driver: HarnessDriver | None = None,
        workspaces: FakeWorkspaceManager | None = None,
    ) -> ApplicationRig:
        store = SQLiteStateStore()
        stores.append(store)
        workspace_manager = workspaces or FakeWorkspaceManager()
        run_numbers = count(1)
        harness = driver or FakeHarnessDriver(
            result=RunResult(
                outcome=RunOutcome.COMPLETED,
                summary="accepted artifact",
                commit="a" * 40,
                consumed_quota=7,
            ),
            id_factory=lambda: f"fake-run-{next(run_numbers)}",
        )
        coordinator = SchedulerCoordinator(
            store,
            workspace_manager,
            DriverRegistry((harness,)),
        )
        return ApplicationRig(
            store=store,
            workspaces=workspace_manager,
            driver=harness,
            plane=ControlPlane(store, coordinator=coordinator),
        )

    yield factory

    for store in stores:
        store.close()


@pytest.fixture
def make_application_job() -> Callable[..., Job]:
    def factory(**overrides: object) -> Job:
        values: dict[str, object] = {
            "id": "job-1",
            "project": "application-tests",
            "repository": "/repositories/example",
            "objective": "exercise the complete local lifecycle",
            "quota_budget": QuotaBudget(
                implementation=6,
                review=1,
                repair=2,
                validation=1,
            ),
            "effort": EffortEstimate(p50=5, p90=10, p99=20),
            "resources": ResourceVector(cpu=2, ram_gb=4),
            "acceptance_criteria": ("the vertical slice completes",),
            "created_at": FIXED_TIME,
            "updated_at": FIXED_TIME,
        }
        values.update(overrides)
        return Job(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def application_node() -> WorkerNode:
    return WorkerNode(
        id="node-1",
        labels={"os": "linux", "arch": "x86_64"},
        capacity=ResourceVector(cpu=8, ram_gb=32),
        harnesses=frozenset({"fake"}),
        updated_at=FIXED_TIME,
    )


@pytest.fixture
def application_quota_pool() -> QuotaPool:
    return QuotaPool(
        id="default",
        provider="fake-provider",
        remaining=100,
        minimum_interactive_reserve=10,
        updated_at=FIXED_TIME,
    )
