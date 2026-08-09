"""Workspace allocation interfaces.

Workspace managers own execution isolation. They do not merge worker output or
otherwise update a repository's integration branch.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentd.domain.models import Job, WorkspaceLease


class WorkspaceError(RuntimeError):
    """Base class for workspace lifecycle failures."""


class WorkspaceAllocationError(WorkspaceError):
    """Raised when a workspace lease cannot be allocated safely."""


class WorkspaceReleaseError(WorkspaceError):
    """Raised when a workspace lease cannot be released safely."""


class WorkspaceCommitError(WorkspaceError):
    """Raised when a trusted workspace handoff cannot be committed safely."""


@runtime_checkable
class WorkspaceManager(Protocol):
    """Allocate exclusive workspaces for write-capable jobs."""

    def allocate(self, job: Job, base_ref: str = "HEAD") -> WorkspaceLease:
        """Create and return an exclusive workspace for ``job``."""
        ...

    def is_available(self, lease: WorkspaceLease) -> bool:
        """Return whether a leased workspace still has its expected ownership."""
        ...

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        """Release scarce workspace resources while preserving the handoff commit."""
        ...

    def current_commit(self, lease: WorkspaceLease) -> str:
        """Return the commit currently associated with ``lease``."""
        ...

    def commit_changes(self, lease: WorkspaceLease) -> str:
        """Commit all nonignored lease changes using the automation identity."""
        ...
