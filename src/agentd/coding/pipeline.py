"""Trusted handoff from completed coding runs to independent publication."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentd.coding.models import RepositoryProfile
from agentd.domain.enums import JobState, RunOutcome, RunState
from agentd.domain.models import CodingOperation
from agentd.intake.models import SourceIssue
from agentd.publication import (
    CollectedCodingResult,
    DraftPublisher,
    PublicationError,
    PublicationIntent,
    ensure_authorized_base,
    import_coding_bundle,
)
from agentd.state.sqlite import SQLiteStateStore


class CodingPublicationReconciler:
    def __init__(
        self,
        store: SQLiteStateStore,
        publisher: DraftPublisher,
        profiles: Mapping[str, RepositoryProfile],
        repositories: Mapping[str, Path],
        base_branches: Mapping[str, str],
        *,
        source_refresh: Callable[[SourceIssue], object] | None = None,
    ) -> None:
        self.store, self.publisher = store, publisher
        self.profiles, self.repositories = dict(profiles), dict(repositories)
        self.base_branches = dict(base_branches)
        self.source_refresh = source_refresh

    async def reconcile(self) -> tuple[dict[str, Any], ...]:
        # Publisher has no execution callback. Failed GitHub calls leave REVIEW
        # and the immutable collected result intact across controller restarts.
        results = []
        for job in self.store.list_jobs(frozenset({JobState.REVIEW})):
            if not isinstance(job.operation, CodingOperation):
                continue
            # A published ledger row is terminal.  It may outlive the profile
            # that produced it (for example after a Mac-to-Linux deployment
            # change), so do not reconstruct the old intent or revalidate it.
            # The publication store is the durable source of the PR summary;
            # returning it also keeps restart reconciliation observable.
            publication = self.publisher.store.get(job.id)
            if publication is not None and publication["stage"] == "published":
                results.append(
                    {
                        "job_id": job.id,
                        "publication_stage": "published",
                        "pr": publication["pr"],
                    }
                )
                continue
            try:
                results.append(await asyncio.to_thread(self.publish_job, job.id))
            except Exception as error:
                results.append(
                    {"job_id": job.id, "publication_error": type(error).__name__}
                )
        return tuple(results)

    def publish_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        run = self.store.latest_run(job_id)
        if (
            job.state is not JobState.REVIEW
            or run is None
            or run.result is None
            or run.result.outcome is not RunOutcome.COMPLETED
            or run.state is not RunState.COMPLETED
            or not isinstance(job.operation, CodingOperation)
            or not isinstance(run.contract.operation, CodingOperation)
        ):
            raise PublicationError("Coding job has no completed review handoff")
        order = run.contract.operation.work_order
        if (
            replace(order, resume_from_run_id=None, prior_consumed_quota=0)
            != job.operation.work_order
        ):
            raise PublicationError(
                "Coding continuation changed the approved work order"
            )
        profile = self.profiles[order.profile_id]
        order.validate_profile(profile)
        source = self.store.github_source_for_job(job.id)
        if (
            source is None
            or source["revoked"]
            or not source["eligible"]
            or source["approved_revision"] != order.source_revision
            or source["revision"] != order.source_revision
        ):
            raise PublicationError("Source authorization changed before publication")
        if self.store.find_active_run(job.id) is not None:
            raise PublicationError("Coding execution ownership is unresolved")
        evidence = run.result.metadata.get("coding_evidence")
        if not isinstance(evidence, dict) or not run.result.commit:
            raise PublicationError("Authenticated coding result lacks Git evidence")
        if evidence.get("profile_digest") != profile.digest:
            raise PublicationError("Collected result profile does not match policy")
        issue = SourceIssue.from_dict(json.loads(source["payload"]))
        if (
            issue.repository != order.repository
            or issue.revision != order.source_revision
        ):
            raise PublicationError("Source identity does not match collected work")
        intent = PublicationIntent(
            job_id=job.id,
            repository=order.repository,
            issue_number=issue.number,
            source_revision=order.source_revision,
            base_branch=self.base_branches[order.repository],
            base_commit=order.base_commit,
            result_commit=run.result.commit,
            worker_id=run.node_id,
            run_id=run.id,
            profile_version=profile.version,
            validation_commands=profile.validation_commands,
            validation_timeout_seconds=profile.validation_timeout_seconds,
        )
        collected = CollectedCodingResult(
            job.id,
            order.repository,
            order.base_commit,
            run.result.commit,
            run.node_id,
            run.id,
            True,
        )
        cache = self.repositories[order.repository]
        ensure_authorized_base(
            cache,
            profile.repository,
            profile.clone_url,
            intent.base_commit,
        )
        import_coding_bundle(intent, collected, evidence, cache)

        def authorized() -> None:
            if self.source_refresh is not None:
                self.source_refresh(issue)
            current = self.store.github_source_for_job(job.id)
            if (
                self.store.get_job(job.id) != job
                or self.store.latest_run(job.id) != run
                or current is None
                or current["revoked"]
                or not current["eligible"]
                or current["revision"] != order.source_revision
                or current["approved_revision"] != order.source_revision
            ):
                raise PublicationError(
                    "Source authorization changed before publication"
                )

        return self.publisher.publish(
            intent, collected, cache, authorization_check=authorized
        )
