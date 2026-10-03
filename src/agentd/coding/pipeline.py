"""Trusted handoff from completed coding runs to independent publication."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentd.coding.models import RepositoryProfile
from agentd.coordinator import LifecycleError, SchedulerCoordinator
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
            latest = self.store.latest_run(job.id)
            if (
                publication is not None
                and publication["stage"] == "published"
                and latest is not None
                and publication["intent"]["run_id"] == latest.id
            ):
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
            replace(
                order,
                resume_from_run_id=None,
                prior_consumed_quota=0,
                repair_context=(),
            )
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
            or self.store.github_job_held(job.id)
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
                or self.store.github_job_held(job.id)
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


class CodingRepairReconciler:
    """Controller-owned feedback loop; publication itself never starts coding."""

    def __init__(
        self,
        store: SQLiteStateStore,
        publications: CodingPublicationReconciler,
        coordinator: SchedulerCoordinator,
        *,
        maximum_attempts: int = 3,
    ) -> None:
        if (
            not isinstance(maximum_attempts, int)
            or isinstance(maximum_attempts, bool)
            or not 1 <= maximum_attempts <= 100
        ):
            raise ValueError("Automatic coding attempts must be bounded")
        self.store, self.publications, self.coordinator = (
            store,
            publications,
            coordinator,
        )
        self.maximum_attempts = maximum_attempts
        self.ledger = publications.publisher.store

    async def reconcile(self) -> tuple[dict[str, Any], ...]:
        results = []
        for job in self.store.list_jobs(frozenset({JobState.FAILED})):
            run = self.store.latest_run(job.id)
            if (
                not isinstance(job.operation, CodingOperation)
                or run is None
                or run.result is None
                or run.result.metadata.get("provider_started") is not False
                or run.result.metadata.get("telemetry_valid") is not True
                or run.result.usage is None
                or run.result.usage.total_tokens != 0
            ):
                continue
            try:
                result = await self._request(
                    job.id,
                    ("Retry the proven pre-provider preparation failure",),
                    actor="trusted-recovery",
                    event_id=f"preparation:{run.id}",
                )
            except Exception:
                result = {
                    "job_id": job.id,
                    "repair": self.ledger.repair_for(job.id, run.id),
                }
            results.append(result)
        for job in self.store.list_jobs(frozenset({JobState.REVIEW})):
            publication = self.ledger.get(job.id)
            run = self.store.latest_run(job.id)
            if (
                not isinstance(job.operation, CodingOperation)
                or publication is None
                or publication["stage"] != "validation_failed"
                or run is None
                or publication["intent"]["run_id"] != run.id
            ):
                continue
            evidence = publication["evidence"] or []
            latest_attempt = max(
                (item.get("validation_attempt", 1) for item in evidence), default=1
            )
            diagnostics = tuple(
                json.dumps(
                    {
                        **{
                            key: value
                            for key, value in item.items()
                            if key not in {"stdout", "stderr"}
                        },
                        "stdout": item.get("stdout", "")[-2048:],
                        "stderr": item.get("stderr", "")[-4096:],
                    },
                    sort_keys=True,
                )[:8192]
                for item in evidence
                if item.get("validation_attempt", 1) == latest_attempt
                and item.get("returncode") != 0
            )[:16]
            try:
                result = await self._request(
                    job.id,
                    diagnostics or ("Independent trusted validation failed",),
                    actor="trusted-validation",
                    event_id=f"validation:{run.id}",
                )
            except Exception:
                result = {
                    "job_id": job.id,
                    "repair": self.ledger.repair_for(job.id, run.id),
                }
            results.append(result)
        return tuple(results)

    async def request_feedback(
        self,
        job_id: str,
        instruction: str,
        *,
        actor: str,
        event_id: str,
    ) -> dict[str, Any]:
        """Only called after the GitHub workflow authenticates actor and event."""
        if not instruction.strip() or "\0" in instruction or len(instruction) > 8192:
            raise LifecycleError("GitHub feedback must be nonempty and bounded")
        return await self._request(
            job_id, (instruction,), actor=actor, event_id=event_id
        )

    async def _request(
        self,
        job_id: str,
        diagnostics: tuple[str, ...],
        *,
        actor: str,
        event_id: str,
    ) -> dict[str, Any]:
        reason = f"Bounded coding repair requested by {actor}; event {event_id}"
        if any(
            transition.to_state is JobState.READY and transition.reason == reason
            for transition in self.store.list_transitions(job_id)
        ):
            # The transition and its event are committed with the job. A
            # controller crash before acknowledging the GitHub event must not
            # queue another coding run or authorize another publication update.
            return {
                "job_id": job_id,
                "state": self.store.get_job(job_id).state.value,
                "repair_status": "queued",
            }
        run = self.store.latest_run(job_id)
        if run is None:
            raise LifecycleError("Coding feedback has no prior execution")
        try:
            if getattr(self.store, "github_job_held", lambda _: False)(job_id):
                raise LifecycleError("Coding repair source is held or unauthorized")
            source = self.store.github_source_for_job(job_id)
            if source is None:
                raise LifecycleError("Coding feedback has no source authorization")
            if self.publications.source_refresh is not None:
                await asyncio.to_thread(
                    self.publications.source_refresh,
                    SourceIssue.from_dict(json.loads(source["payload"])),
                )
            publication = self.ledger.get(job_id)
            if publication is not None and publication["stage"] == "published":
                self.ledger.authorize_update(
                    job_id,
                    publication["intent"]["run_id"],
                    event_id=event_id,
                )
            elif (
                publication is not None and publication["stage"] != "validation_failed"
            ):
                raise PublicationError(
                    "Prior publication or validation must reconcile before feedback"
                )
            queued = await self.coordinator.queue_coding_repair(
                job_id,
                diagnostics=diagnostics,
                maximum_attempts=self.maximum_attempts,
                actor=actor,
                event_id=event_id,
            )
            self.ledger.record_repair(
                job_id,
                run.id,
                "queued",
                f"Feedback {event_id} queued under unchanged cumulative limits",
            )
            return {
                "job_id": job_id,
                "state": queued.state.value,
                "repair_status": "queued",
            }
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, (LifecycleError, PublicationError))
                else type(error).__name__
            )
            outcome = (
                "exhausted" if "limit" in reason or "exhausted" in reason else "blocked"
            )
            self.ledger.record_repair(job_id, run.id, outcome, reason)
            raise
