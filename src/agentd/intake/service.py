"""Internal integration: reconcile sources, then use the existing scheduler."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Protocol

from agentd.domain.models import Job
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore


class IssueSource(Protocol):
    def poll(self, repository: str) -> tuple[SourceIssue, ...]: ...
    def get(self, repository: str, number: int) -> SourceIssue: ...


class GitHubIntake:
    def __init__(
        self,
        store: SQLiteStateStore,
        source: IssueSource,
        policies: tuple[IntakePolicy, ...],
        compile_job: Callable[[SourceIssue], Job],
        control_plane: ControlPlane,
    ) -> None:
        self.store = store
        self.source = source
        self.policies = {policy.repository: policy for policy in policies}
        self.compile_job = compile_job
        self.control_plane = control_plane

    async def poll(self) -> tuple[str, ...]:
        observed = []
        for policy in self.policies.values():
            for issue in await asyncio.to_thread(self.source.poll, policy.repository):
                await self.reconcile(issue)
                observed.append(issue.key)
        # Refresh every known job directly: pagination cannot hide closure/removal.
        for job in self.store.list_jobs():
            if job.terminal:
                continue
            record = self.store.github_source_for_job(job.id)
            if record is None:
                continue
            old = SourceIssue.from_dict(json.loads(record["payload"]))
            policy = self.policies.get(old.repository)
            if policy is None:
                await self.control_plane.cancel(job.id)
                continue
            current = await asyncio.to_thread(
                self.source.get, old.repository, old.number
            )
            await self.reconcile(current)
        return tuple(observed)

    async def reconcile(self, issue: SourceIssue) -> Job | None:
        policy = self.policies.get(issue.repository)
        if policy is None:
            raise ValueError("repository is not allowlisted")
        decision = self.store.observe_github_issue(issue, policy)
        try:
            job = self.store.get_job(issue.job_id)
        except LookupError:
            job = None
        if decision == "approved":
            if job is not None and job.state.value != "BACKLOG":
                return job
            return self.store.create_github_job(issue, self.compile_job(issue))
        if job is not None and not job.terminal and self.store.list_runs(job.id):
            # Existing coordinator preserves unresolved remote ownership/resources.
            return await self.control_plane.cancel(job.id)
        return job

    def approve(self, repository: str, number: int, *, actor: str) -> SourceIssue:
        policy = self.policies[repository]
        issue = self.source.get(repository, number)
        self.store.observe_github_issue(issue, policy)
        self.store.approve_github_issue(issue, policy, actor=actor)
        return issue

    def refresh_authorization(self, issue: SourceIssue) -> None:
        """Recheck live authority at the publication boundary, including identity."""
        policy = self.policies[issue.repository]
        current = self.source.get(issue.repository, issue.number)
        self.store.observe_github_issue(current, policy)
        if (
            current.key != issue.key
            or current.revision != issue.revision
            or not policy.eligible(current)
        ):
            raise ValueError("Source authorization changed before publication")
