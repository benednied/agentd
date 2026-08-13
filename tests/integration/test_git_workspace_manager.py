from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agentd.domain.enums import WorkspaceState
from agentd.domain.models import EffortEstimate, Job, QuotaBudget
from agentd.workspaces import (
    GitWorkspaceManager,
    WorkspaceAllocationError,
    WorkspaceCommitError,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceReleaseError,
)


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


def _job(repository: Path, *, job_id: str = "job-1") -> Job:
    return Job(
        id=job_id,
        project="workspace-tests",
        repository=str(repository),
        objective="Exercise Git workspace isolation",
        effort=EffortEstimate(p50=1, p90=2),
        quota_budget=QuotaBudget(implementation=1),
    )


def test_allocate_isolates_worker_branch_from_default_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    worktree_root = tmp_path / "worktrees"
    manager = GitWorkspaceManager(worktree_root)
    assert isinstance(manager, WorkspaceManager)
    main_commit = _git(repository, "rev-parse", "HEAD")

    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)

    assert lease.state is WorkspaceState.LEASED
    assert working_directory.parent == worktree_root.resolve()
    assert working_directory.is_dir()
    assert _git(repository, "branch", "--show-current") == "main"
    assert _git(repository, "rev-parse", "HEAD") == main_commit
    assert _git(working_directory, "branch", "--show-current") == lease.branch
    assert manager.current_commit(lease) == main_commit


def test_release_records_commit_retains_branch_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    main_commit = _git(repository, "rev-parse", "HEAD")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)

    (working_directory / "result.txt").write_text("accepted artifact\n")
    _git(working_directory, "add", "result.txt")
    _git(working_directory, "commit", "-m", "produce result")
    worker_commit = _git(working_directory, "rev-parse", "HEAD")

    released = manager.release(lease)

    assert released.state is WorkspaceState.RELEASED
    assert released.released_at is not None
    assert released.commit == worker_commit
    assert not working_directory.exists()
    assert _git(repository, "rev-parse", "HEAD") == main_commit
    assert _git(repository, "rev-parse", released.branch) == worker_commit
    assert manager.current_commit(released) == worker_commit
    assert manager.release(released) is released
    assert manager.release(lease).commit == worker_commit


def test_trusted_commit_captures_nonignored_changes_with_fixed_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "obsolete.txt").write_text("remove me\n")
    _git(repository, "add", "obsolete.txt")
    _git(repository, "commit", "-m", "add obsolete fixture")
    main_commit = _git(repository, "rev-parse", "HEAD")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)

    (working_directory / "README.md").write_text("updated\n")
    (working_directory / "obsolete.txt").unlink()
    (working_directory / "result.txt").write_text("review me\n")
    (working_directory / ".gitignore").write_text("ignored.log\n")
    (working_directory / "ignored.log").write_text("do not commit\n")

    # A repository-controlled hook must not execute in the trusted service path.
    hook_sentinel = tmp_path / "hook-ran"
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {hook_sentinel}\nexit 91\n")
    hook.chmod(0o755)

    commit = manager.commit_changes(lease)

    assert commit != main_commit
    assert _git(working_directory, "show", "-s", "--format=%s", commit) == (
        "agentd: capture review handoff"
    )
    assert _git(working_directory, "show", "-s", "--format=%an <%ae>", commit) == (
        "agentd automation <agentd@localhost>"
    )
    assert _git(working_directory, "show", "-s", "--format=%cn <%ce>", commit) == (
        "agentd automation <agentd@localhost>"
    )
    assert _git(working_directory, "status", "--short") == ""
    assert "ignored.log" not in _git(working_directory, "ls-files").splitlines()
    assert not hook_sentinel.exists()
    assert _git(repository, "rev-parse", "HEAD") == main_commit


def test_trusted_commit_excludes_control_plane_runtime_scratch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)

    (working_directory / "result.txt").write_text("review me\n")
    (working_directory / ".uv-cache").mkdir()
    (working_directory / ".uv-cache" / "wheel").write_text("scratch\n")
    (working_directory / "pytest-of-worker").mkdir()
    (working_directory / "pytest-of-worker" / "state").write_text("scratch\n")

    commit = manager.commit_changes(lease)

    assert "result.txt" in _git(
        working_directory, "ls-tree", "-r", "--name-only", commit
    )
    assert ".uv-cache" not in _git(
        working_directory, "ls-tree", "-r", "--name-only", commit
    )
    assert "pytest-of-worker" not in _git(
        working_directory, "ls-tree", "-r", "--name-only", commit
    )

    commit_count = _git(working_directory, "rev-list", "--count", "HEAD")
    assert manager.commit_changes(lease) == commit
    assert _git(working_directory, "rev-list", "--count", "HEAD") == commit_count


def test_trusted_commit_validates_registration_before_staging(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)
    (working_directory / "result.txt").write_text("must remain unstaged\n")
    git_directory = Path(_git(working_directory, "rev-parse", "--git-dir"))
    registration = git_directory / "gitdir"
    original_registration = registration.read_text()
    registration.write_text(str(tmp_path / "different-worktree" / ".git") + "\n")

    try:
        with pytest.raises(WorkspaceCommitError, match="registered Git worktree"):
            manager.commit_changes(lease)
    finally:
        registration.write_text(original_registration)

    assert _git(working_directory, "diff", "--cached", "--name-only") == ""
    assert _git(working_directory, "status", "--short") == "?? result.txt"


def test_job_names_cannot_escape_root_or_create_invalid_refs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    worktree_root = tmp_path / "worktrees"
    manager = GitWorkspaceManager(worktree_root, branch_prefix="agentd/work")
    job = _job(repository, job_id="../../ bad @{ ref.lock \\ name")

    lease = manager.allocate(job)
    relative_path = Path(lease.working_directory).relative_to(worktree_root.resolve())

    assert len(relative_path.parts) == 1
    assert lease.branch.startswith("agentd/work/")
    assert ".." not in lease.branch
    assert "@{" not in lease.branch
    assert " " not in lease.branch
    assert "\\" not in lease.branch
    _git(repository, "check-ref-format", "--branch", lease.branch)


def test_release_refuses_to_destroy_uncommitted_output(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)
    output = working_directory / "uncommitted.txt"
    output.write_text("do not discard\n")

    with pytest.raises(WorkspaceReleaseError):
        manager.release(lease)

    assert output.read_text() == "do not discard\n"
    assert working_directory.exists()


@pytest.mark.parametrize(
    "forged_identity",
    (
        {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
        {"job_id": "attacker-job"},
    ),
)
def test_forged_lease_identity_cannot_read_or_release_victim_worktree(
    tmp_path: Path,
    forged_identity: dict[str, str],
) -> None:
    repository = _repository(tmp_path)
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    victim = manager.allocate(_job(repository))
    victim_directory = Path(victim.working_directory)
    victim_commit = _git(victim_directory, "rev-parse", "HEAD")
    forged = replace(victim, **forged_identity)

    with pytest.raises(WorkspaceError, match="does not belong"):
        manager.current_commit(forged)
    with pytest.raises(WorkspaceCommitError, match="does not belong"):
        manager.commit_changes(forged)
    with pytest.raises(WorkspaceReleaseError, match="does not belong"):
        manager.release(forged)

    assert victim_directory.is_dir()
    assert manager.current_commit(victim) == victim_commit
    assert manager.is_available(victim)


def test_failed_worktree_creation_rolls_back_owned_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    class FailingAddManager(GitWorkspaceManager):
        def _add_worktree(
            self,
            repository: Path,
            working_directory: Path,
            branch: str,
        ) -> None:
            raise OSError("injected worktree creation failure")

    worktree_root = tmp_path / "worktrees"
    manager = FailingAddManager(worktree_root)

    with pytest.raises(WorkspaceAllocationError, match="injected"):
        manager.allocate(_job(repository))

    refs = _git(
        repository,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/agentd",
    )
    assert refs == ""
    assert not any(worktree_root.iterdir())
