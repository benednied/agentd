from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from agentd.domain.models import EffortEstimate, Job, QuotaBudget
from agentd.workspaces import GitWorkspaceManager


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> Path:
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(path)),
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "config", "user.name", "Agentd Tests")
    _git(path, "config", "user.email", "agentd-tests@example.invalid")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _job(repository: Path) -> Job:
    return Job(
        id="availability-job",
        project="workspace-availability",
        repository=str(repository),
        objective="Validate an existing workspace lease",
        effort=EffortEstimate(p50=1, p90=2),
        quota_budget=QuotaBudget(implementation=1),
    )


def test_only_live_leased_worktree_is_available(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))

    assert manager.is_available(lease)

    released = manager.release(lease)
    assert not manager.is_available(released)
    # A stale persisted LEASED snapshot is also unavailable after its path is gone.
    assert not manager.is_available(lease)
    assert manager.current_commit(released) == released.commit


def test_missing_worktree_is_false_without_pruning_or_deleting_output(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)
    output = working_directory / "uncommitted.txt"
    output.write_text("preserve me\n")
    registration_before = _git(repository, "worktree", "list", "--porcelain")
    registered_path = f"worktree {working_directory}"
    assert registered_path in registration_before

    moved_directory = manager.root / "moved-with-output"
    working_directory.rename(moved_directory)

    assert not manager.is_available(lease)
    assert (moved_directory / output.name).read_text() == "preserve me\n"
    # Merely listing a missing worktree adds a dynamic ``prunable`` annotation;
    # the registration itself must remain until an explicit cleanup operation.
    assert registered_path in _git(repository, "worktree", "list", "--porcelain")


def test_changed_branch_is_stale_but_validation_does_not_change_it(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    working_directory = Path(lease.working_directory)
    _git(working_directory, "switch", "-c", "worker-changed-branch")

    assert not manager.is_available(lease)
    assert working_directory.is_dir()
    assert _git(working_directory, "branch", "--show-current") == (
        "worker-changed-branch"
    )


def test_worktree_must_belong_to_the_lease_repository(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    other_repository = _repository(tmp_path / "other-repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    wrong_repository = replace(lease, repository=str(other_repository))

    assert not manager.is_available(wrong_repository)
    assert manager.is_available(lease)


def test_lease_identity_must_match_its_job_branch_and_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))

    assert not manager.is_available(replace(lease, job_id="different-job"))
    assert not manager.is_available(replace(lease, id="different-lease"))
    assert manager.is_available(lease)


def test_copied_checkout_is_not_the_registered_owned_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    manager = GitWorkspaceManager(tmp_path / "worktrees")
    lease = manager.allocate(_job(repository))
    token = lease.id.replace("-", "")[:12]
    copied_path = manager.root / f"copy-availability-job-{token}"
    shutil.copytree(lease.working_directory, copied_path)
    copied_lease = replace(lease, working_directory=str(copied_path))

    assert not manager.is_available(copied_lease)
    assert copied_path.is_dir()
    assert manager.is_available(lease)
