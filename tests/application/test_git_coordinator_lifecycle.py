from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    EffortEstimate,
    Job,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    ResumeCapsule,
    RunResult,
    WorkerNode,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workspaces import GitWorkspaceManager


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repository, "config", "user.name", "Agentd Tests")
    _git(repository, "config", "user.email", "agentd-tests@example.invalid")
    (repository / "README.md").write_text("initial\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_coordinator_reuses_then_releases_real_git_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    main_commit = _git(repository, "rev-parse", "HEAD")
    workspace_manager = GitWorkspaceManager(tmp_path / "worktrees")
    driver = FakeHarnessDriver(id_factory=iter(("attempt-1", "attempt-2")).__next__)
    store = SQLiteStateStore(tmp_path / "agentd.sqlite")
    coordinator = SchedulerCoordinator(
        store,
        workspace_manager,
        DriverRegistry((driver,)),
    )
    plane = ControlPlane(store, coordinator=coordinator)
    job = Job(
        id="git-job",
        project="git-lifecycle",
        repository=str(repository),
        objective="produce a committed artifact in an isolated worktree",
        effort=EffortEstimate(p50=1, p90=2),
        quota_budget=QuotaBudget(implementation=4),
        resources=ResourceVector(cpu=1, ram_gb=1),
    )
    node = WorkerNode(
        id="local-node",
        labels={"os": "macos", "arch": "arm64"},
        capacity=ResourceVector(cpu=4, ram_gb=8),
        harnesses=frozenset({"fake"}),
    )
    pool = QuotaPool(id="default", provider="fake", remaining=20)
    capsule = ResumeCapsule(
        completed=("workspace allocated",),
        current=("artifact",),
        next_steps=("commit", "validate"),
    )

    async def scenario() -> None:
        plane.register_node(node)
        plane.register_quota_pool(pool)
        plane.submit(job)

        first_run = await plane.dispatch_next()
        assert first_run is not None
        first_workspace = plane.inspect_workspace(job.id)
        assert first_workspace is not None
        working_directory = Path(first_workspace.working_directory)
        assert working_directory.is_dir()
        assert _git(working_directory, "branch", "--show-current") == (
            first_workspace.branch
        )

        assert (await plane.pause(job.id, capsule)).state is JobState.SUSPENDED
        assert (await plane.resume(job.id)).state is JobState.READY
        second_run = await plane.dispatch_next()
        assert second_run is not None
        assert second_run.workspace_id == first_run.workspace_id
        assert second_run.contract.resume == replace(capsule, commit=main_commit)

        (working_directory / "artifact.txt").write_text("accepted\n")
        _git(working_directory, "add", "artifact.txt")
        _git(working_directory, "commit", "-m", "produce accepted artifact")
        worker_commit = _git(working_directory, "rev-parse", "HEAD")
        driver.configure_result(
            second_run.handle,
            RunResult(
                outcome=RunOutcome.COMPLETED,
                summary="committed artifact",
                commit=worker_commit,
                consumed_quota=2,
            ),
        )

        assert (await plane.complete(job.id)).state is JobState.COMPLETED

        released = plane.inspect_workspace(job.id)
        assert released is not None
        assert released.state is WorkspaceState.RELEASED
        assert released.commit == worker_commit
        assert not working_directory.exists()
        assert _git(repository, "branch", "--show-current") == "main"
        assert _git(repository, "rev-parse", "HEAD") == main_commit
        assert _git(repository, "rev-parse", released.branch) == worker_commit
        assert [run.state for run in plane.runs(job.id)] == [
            RunState.SUSPENDED,
            RunState.COMPLETED,
        ]
        assert all(
            item.state is AllocationState.RELEASED
            for item in store.list_allocations(job.id)
        )
        assert all(
            item.state is ReservationState.RELEASED
            for item in store.list_reservations(job.id)
        )
        assert plane.inspect_quota("default").remaining == 18

    try:
        asyncio.run(scenario())
    finally:
        store.close()
