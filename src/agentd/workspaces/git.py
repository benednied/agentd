"""Git-worktree-backed workspace isolation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agentd.domain.enums import WorkspaceState
from agentd.domain.models import Job, WorkspaceLease
from agentd.observability import event_logger
from agentd.workspaces.base import (
    WorkspaceAllocationError,
    WorkspaceCommitError,
    WorkspaceError,
    WorkspaceReleaseError,
)

_INVALID_BRANCH_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATED_DOTS = re.compile(r"\.{2,}")
_COMMIT_ID = re.compile(r"[0-9a-fA-F]{40,64}")
_AUTOMATION_NAME = "agentd automation"
_AUTOMATION_EMAIL = "agentd@localhost"
_AUTOMATION_COMMIT_MESSAGE = "agentd: capture review handoff"
_RUNTIME_SCRATCH_PATHS = (
    ":(exclude).uv-cache/**",
    ":(exclude)pytest-of-*",
    ":(exclude)pytest-of-*/**",
)


class _GitCommandError(RuntimeError):
    def __init__(
        self,
        arguments: tuple[str, ...],
        returncode: int | None,
        detail: str,
    ) -> None:
        self.arguments = arguments
        self.returncode = returncode
        self.detail = detail
        rendered = " ".join(arguments)
        status = "timed out" if returncode is None else f"exited with {returncode}"
        super().__init__(f"{rendered} {status}: {detail}".rstrip())


def sanitize_branch_component(value: str, *, fallback: str = "item") -> str:
    """Return a conservative Git-ref and path component.

    The result contains only ASCII letters, numbers, dots, underscores, and
    hyphens. Git's special ``..`` and ``.lock`` forms are eliminated as well.
    """

    def clean(candidate: str) -> str:
        normalized = unicodedata.normalize("NFKD", candidate)
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        result = _INVALID_BRANCH_CHARACTERS.sub("-", ascii_value)
        result = _REPEATED_DOTS.sub(".", result).strip("._-")
        if result.lower().endswith(".lock"):
            result = f"{result[:-5]}-lock"
        return result[:48].rstrip("._-")

    return clean(value) or clean(fallback) or "item"


class GitWorkspaceManager:
    """Allocate one exclusive Git branch and worktree per lease.

    Worker branches are retained after release. This preserves a stable ref for
    the immutable commit handoff while the worktree itself is reclaimed.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        branch_prefix: str = "agentd",
        git_executable: str = "git",
        command_timeout_seconds: float = 30,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self._root = Path(root).expanduser().resolve()
        prefix_parts = (
            sanitize_branch_component(part, fallback="agentd")
            for part in branch_prefix.split("/")
            if part
        )
        self._branch_prefix = "/".join(prefix_parts) or "agentd"
        self._git_executable = git_executable
        self._command_timeout_seconds = command_timeout_seconds

    @property
    def root(self) -> Path:
        """Configured root under which all leased worktrees are created."""

        return self._root

    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        """Allocate a branch and linked worktree without touching the base branch."""

        repository: Path | None = None
        working_directory: Path | None = None
        branch: str | None = None
        branch_created = False
        working_directory_created = False
        try:
            repository = self._repository_root(job.repository)
            base_commit = self._resolve_commit(repository, base_ref)
            self._root.mkdir(parents=True, exist_ok=True)

            lease_id = str(uuid4())
            token = lease_id.replace("-", "")[:12]
            project = sanitize_branch_component(job.project, fallback="project")
            job_id = sanitize_branch_component(job.id, fallback="job")
            branch = f"{self._branch_prefix}/{project}-{job_id}-{token}"
            working_directory = self._root / f"{project}-{job_id}-{token}"
            self._assert_within_root(working_directory)
            # Atomic creation establishes ownership of this path. Rollback may
            # only remove paths which this allocation call successfully made.
            working_directory.mkdir()
            working_directory_created = True

            # Creating the ref separately makes ownership unambiguous. If the
            # subsequent worktree operation fails, only this newly created ref
            # is eligible for rollback.
            self._run_git(repository, "branch", branch, base_commit)
            branch_created = True
            self._add_worktree(repository, working_directory, branch)
            self._assert_expected_worktree(repository, working_directory, branch)
            commit = self._commit_at_path(working_directory)
            lease = WorkspaceLease(
                id=lease_id,
                job_id=job.id,
                repository=str(repository),
                branch=branch,
                working_directory=str(working_directory),
                base_ref=base_ref,
                commit=commit,
            )
            event_logger(
                component="workspace",
                operation="allocate",
                job_id=job.id,
                workspace_id=lease.id,
            ).info("workspace_allocated")
            return lease
        except WorkspaceAllocationError:
            self._rollback_allocation(
                repository,
                working_directory,
                branch,
                branch_created=branch_created,
                working_directory_created=working_directory_created,
            )
            raise
        except (OSError, _GitCommandError, WorkspaceError) as exc:
            rollback_errors = self._rollback_allocation(
                repository,
                working_directory,
                branch,
                branch_created=branch_created,
                working_directory_created=working_directory_created,
            )
            suffix = (
                f" Rollback also reported: {'; '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            event_logger(
                component="workspace",
                operation="allocate",
                job_id=job.id,
                error_type=type(exc).__name__,
            ).error("workspace_allocation_failed")
            raise WorkspaceAllocationError(
                f"Could not allocate a Git workspace for job {job.id}: {exc}.{suffix}"
            ) from exc

    def is_available(self, lease: WorkspaceLease) -> bool:
        """Return whether a lease still identifies its owned linked worktree.

        Validation is deliberately observational: it does not prune stale Git
        metadata, remove directories, change branches, or otherwise try to repair
        the lease. A caller can therefore use ``False`` to decide whether a new
        workspace must be allocated without risking worker output.
        """

        if lease.state is not WorkspaceState.LEASED:
            return False

        try:
            repository = self._repository_root(lease.repository)
            working_directory = Path(lease.working_directory).resolve()
            self._assert_within_root(working_directory)
            self._assert_owned_branch(lease.branch)
            self._assert_lease_identity(lease, working_directory)
            if not working_directory.is_dir():
                return False
            self._assert_expected_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            self._assert_registered_worktree(
                repository,
                working_directory,
                lease.branch,
            )
        except (OSError, _GitCommandError, WorkspaceError):
            return False
        return True

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        """Remove a clean worktree and retain its worker branch and final commit."""

        if lease.state is WorkspaceState.RELEASED:
            return lease

        try:
            repository = self._repository_root(lease.repository)
            working_directory = Path(lease.working_directory).resolve()
            self._assert_within_root(working_directory)
            self._assert_owned_branch(lease.branch)
            self._assert_lease_identity(lease, working_directory)

            if working_directory.exists():
                self._assert_expected_worktree(
                    repository, working_directory, lease.branch
                )
                self._assert_registered_worktree(
                    repository, working_directory, lease.branch
                )
                commit = self._commit_at_path(working_directory)
                # Deliberately omit --force: uncommitted worker output must not
                # be destroyed as a side effect of resource reclamation.
                self._run_git(
                    repository,
                    "worktree",
                    "remove",
                    "--",
                    str(working_directory),
                )
            else:
                commit = self._commit_for_missing_worktree(repository, lease)

            self._run_git(repository, "worktree", "prune")
            released = replace(
                lease,
                commit=commit,
                state=WorkspaceState.RELEASED,
                released_at=datetime.now(UTC),
            )
            event_logger(
                component="workspace",
                operation="release",
                job_id=lease.job_id,
                workspace_id=lease.id,
            ).info("workspace_released")
            return released
        except (OSError, _GitCommandError, WorkspaceError) as exc:
            event_logger(
                component="workspace",
                operation="release",
                job_id=lease.job_id,
                workspace_id=lease.id,
                error_type=type(exc).__name__,
            ).error("workspace_release_failed")
            raise WorkspaceReleaseError(
                f"Could not release workspace lease {lease.id}: {exc}"
            ) from exc

    def current_commit(self, lease: WorkspaceLease) -> str:
        """Return the checked-out commit, or the retained branch tip after release."""

        try:
            repository = self._repository_root(lease.repository)
            working_directory = Path(lease.working_directory).resolve()
            self._assert_within_root(working_directory)
            self._assert_owned_branch(lease.branch)
            self._assert_lease_identity(lease, working_directory)
            if working_directory.exists():
                self._assert_expected_worktree(
                    repository, working_directory, lease.branch
                )
                self._assert_registered_worktree(
                    repository, working_directory, lease.branch
                )
                return self._commit_at_path(working_directory)
            return self._commit_for_missing_worktree(repository, lease)
        except (OSError, _GitCommandError, WorkspaceError) as exc:
            raise WorkspaceError(
                f"Could not inspect workspace lease {lease.id}: {exc}"
            ) from exc

    def commit_changes(self, lease: WorkspaceLease) -> str:
        """Create an idempotent trusted commit for all nonignored lease changes.

        The message and identity are deliberately fixed rather than derived from
        model or job text. Repository hooks are disabled for this control-plane
        operation so staging a handoff cannot invoke repository-provided code.
        """

        if lease.state is not WorkspaceState.LEASED:
            raise WorkspaceCommitError(
                f"Workspace lease {lease.id} is not available for a trusted commit"
            )

        try:
            repository = self._repository_root(lease.repository)
            working_directory = Path(lease.working_directory).resolve()
            self._assert_within_root(working_directory)
            self._assert_owned_branch(lease.branch)
            self._assert_lease_identity(lease, working_directory)
            if not working_directory.is_dir():
                raise WorkspaceError(
                    f"Leased worktree does not exist: {working_directory}"
                )
            self._assert_expected_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            self._assert_registered_worktree(
                repository,
                working_directory,
                lease.branch,
            )

            self._run_git(
                working_directory,
                "add",
                "--all",
                "--",
                ".",
                *_RUNTIME_SCRATCH_PATHS,
            )

            # Revalidate ownership after staging and before moving the ref. A
            # failed attempt can be retried safely because the index is retained.
            self._assert_expected_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            self._assert_registered_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            difference = self._run_git(
                working_directory,
                "diff",
                "--cached",
                "--quiet",
                "--exit-code",
                check=False,
            )
            if difference.returncode == 0:
                return self._commit_at_path(working_directory)
            if difference.returncode != 1:
                detail = difference.stderr.strip() or difference.stdout.strip()
                raise _GitCommandError(
                    difference.args,
                    difference.returncode,
                    detail,
                )

            identity = {
                "GIT_AUTHOR_NAME": _AUTOMATION_NAME,
                "GIT_AUTHOR_EMAIL": _AUTOMATION_EMAIL,
                "GIT_COMMITTER_NAME": _AUTOMATION_NAME,
                "GIT_COMMITTER_EMAIL": _AUTOMATION_EMAIL,
            }
            self._run_git(
                working_directory,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                f"user.name={_AUTOMATION_NAME}",
                "-c",
                f"user.email={_AUTOMATION_EMAIL}",
                "commit",
                "--no-gpg-sign",
                "--message",
                _AUTOMATION_COMMIT_MESSAGE,
                environment=identity,
            )
            self._assert_expected_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            self._assert_registered_worktree(
                repository,
                working_directory,
                lease.branch,
            )
            return self._commit_at_path(working_directory)
        except (OSError, _GitCommandError, WorkspaceError) as exc:
            if isinstance(exc, WorkspaceCommitError):
                raise
            raise WorkspaceCommitError(
                f"Could not commit workspace lease {lease.id}: {exc}"
            ) from exc

    def _repository_root(self, repository: str) -> Path:
        candidate = Path(repository).expanduser().resolve()
        result = self._run_git(candidate, "rev-parse", "--show-toplevel")
        root = Path(result.stdout.strip()).resolve()
        if not root.is_dir():
            raise WorkspaceError(f"Git repository does not exist: {root}")
        return root

    def _resolve_commit(self, repository: Path, ref: str) -> str:
        if not ref or ref.startswith("-") or "\0" in ref:
            raise WorkspaceAllocationError(f"Invalid base ref: {ref!r}")
        result = self._run_git(
            repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        )
        return self._parse_commit(result.stdout)

    def _add_worktree(
        self, repository: Path, working_directory: Path, branch: str
    ) -> None:
        self._run_git(
            repository,
            "worktree",
            "add",
            "--",
            str(working_directory),
            branch,
        )

    def _commit_at_path(self, working_directory: Path) -> str:
        result = self._run_git(
            working_directory,
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        )
        return self._parse_commit(result.stdout)

    def _commit_for_missing_worktree(
        self, repository: Path, lease: WorkspaceLease
    ) -> str:
        ref = f"refs/heads/{lease.branch}^{{commit}}"
        result = self._run_git(
            repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            ref,
            check=False,
        )
        if result.returncode == 0:
            return self._parse_commit(result.stdout)
        if lease.commit is not None and _COMMIT_ID.fullmatch(lease.commit):
            return lease.commit.lower()
        raise WorkspaceError(
            f"Neither worktree nor retained branch exists for lease {lease.id}"
        )

    def _assert_expected_worktree(
        self, repository: Path, working_directory: Path, branch: str
    ) -> None:
        top_level = Path(
            self._run_git(
                working_directory, "rev-parse", "--show-toplevel"
            ).stdout.strip()
        ).resolve()
        if top_level != working_directory:
            raise WorkspaceError(
                f"Lease path is not a worktree root: {working_directory}"
            )

        expected_common_dir = self._common_git_directory(repository)
        actual_common_dir = self._common_git_directory(working_directory)
        if actual_common_dir != expected_common_dir:
            raise WorkspaceError(
                f"Lease path belongs to a different Git repository: {working_directory}"
            )

        actual_branch = self._run_git(
            working_directory,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ).stdout.strip()
        if actual_branch != branch:
            raise WorkspaceError(
                f"Lease expected branch {branch!r}, found {actual_branch!r}"
            )

    def _common_git_directory(self, repository: Path) -> Path:
        result = self._run_git(
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        return Path(result.stdout.strip()).resolve()

    def _assert_registered_worktree(
        self,
        repository: Path,
        working_directory: Path,
        branch: str,
    ) -> None:
        result = self._run_git(
            repository,
            "worktree",
            "list",
            "--porcelain",
            "-z",
        )
        expected_branch = f"refs/heads/{branch}"
        for record in self._parse_worktree_records(result.stdout):
            registered_path = record.get("worktree")
            if registered_path is None:
                continue
            if Path(registered_path).resolve() != working_directory:
                continue
            if record.get("branch") != expected_branch:
                raise WorkspaceError(
                    f"Registered worktree does not own branch {branch!r}: "
                    f"{working_directory}"
                )
            return
        raise WorkspaceError(
            f"Lease path is not a registered Git worktree: {working_directory}"
        )

    @staticmethod
    def _parse_worktree_records(output: str) -> tuple[dict[str, str], ...]:
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for field in output.split("\0"):
            if not field:
                if current:
                    records.append(current)
                    current = {}
                continue
            key, separator, value = field.partition(" ")
            current[key] = value if separator else ""
        if current:
            records.append(current)
        return tuple(records)

    def _assert_within_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Workspace path escapes configured root {self._root}: {path}"
            ) from exc
        if path == self._root:
            raise WorkspaceError("A lease cannot target the workspace root itself")

    def _assert_owned_branch(self, branch: str) -> None:
        if not branch.startswith(f"{self._branch_prefix}/"):
            raise WorkspaceError(
                f"Branch {branch!r} is not owned by prefix {self._branch_prefix!r}"
            )

    def _assert_lease_identity(
        self,
        lease: WorkspaceLease,
        working_directory: Path,
    ) -> None:
        token = lease.id.replace("-", "")[:12].lower()
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise WorkspaceError(f"Lease {lease.id!r} has no owned workspace token")
        job_id = sanitize_branch_component(lease.job_id, fallback="job")
        expected_suffix = f"-{job_id}-{token}"
        if not lease.branch.endswith(expected_suffix):
            raise WorkspaceError(
                f"Branch {lease.branch!r} does not belong to lease {lease.id}"
            )
        if not working_directory.name.endswith(expected_suffix):
            raise WorkspaceError(
                f"Workspace path does not belong to lease {lease.id}: "
                f"{working_directory}"
            )

    def _parse_commit(self, output: str) -> str:
        commit = output.strip()
        if not _COMMIT_ID.fullmatch(commit):
            raise WorkspaceError(
                f"Git returned an invalid commit identifier: {commit!r}"
            )
        return commit.lower()

    def _run_git(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = (
            self._git_executable,
            "-C",
            str(repository),
            *arguments,
        )
        process_environment = os.environ.copy()
        process_environment["GIT_TERMINAL_PROMPT"] = "0"
        if environment is not None:
            process_environment.update(environment)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=process_environment,
                timeout=self._command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise _GitCommandError(command, None, "command timed out") from exc
        except OSError as exc:
            raise _GitCommandError(command, None, str(exc)) from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise _GitCommandError(command, result.returncode, detail)
        return result

    def _rollback_allocation(
        self,
        repository: Path | None,
        working_directory: Path | None,
        branch: str | None,
        *,
        branch_created: bool,
        working_directory_created: bool,
    ) -> list[str]:
        errors: list[str] = []
        if repository is None:
            return errors

        if working_directory is not None and working_directory_created:
            try:
                self._run_git(
                    repository,
                    "worktree",
                    "remove",
                    "--force",
                    "--",
                    str(working_directory),
                    check=False,
                )
            except _GitCommandError as exc:
                errors.append(f"worktree removal failed: {exc}")

            if working_directory.exists() or working_directory.is_symlink():
                try:
                    self._assert_within_root(working_directory)
                    if working_directory.is_symlink():
                        working_directory.unlink()
                    else:
                        shutil.rmtree(working_directory)
                except (OSError, WorkspaceError) as exc:
                    errors.append(f"partial directory removal failed: {exc}")

            # Pruning after filesystem cleanup also removes metadata left by a
            # Git failure partway through `worktree add` or `worktree remove`.
            try:
                self._run_git(repository, "worktree", "prune", check=False)
            except _GitCommandError as exc:
                errors.append(f"worktree pruning failed: {exc}")

        if branch_created and branch is not None:
            try:
                result = self._run_git(repository, "branch", "-D", branch, check=False)
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip()
                    errors.append(f"branch removal failed: {detail}")
            except _GitCommandError as exc:
                errors.append(f"branch removal failed: {exc}")
        return errors
