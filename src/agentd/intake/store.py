"""Transactional source provenance alongside the existing job ledger."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import TYPE_CHECKING, Any

from agentd.domain.enums import JobState, QoSClass
from agentd.domain.models import Job
from agentd.domain.transitions import initial_transition, transition_job
from agentd.intake.models import IntakePolicy, SourceIssue

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


def migrate_intake(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS github_sources (
        source_key TEXT PRIMARY KEY, job_id TEXT UNIQUE,
        revision TEXT NOT NULL, approved_revision TEXT, approved_by TEXT,
        approved_at TEXT, eligible INTEGER NOT NULL, revoked INTEGER NOT NULL,
        payload TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS github_source_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL,
        revision TEXT NOT NULL, action TEXT NOT NULL, actor TEXT,
        occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


class IntakeStoreMixin:
    """SQLite extension: source decisions and job creation share one transaction."""

    if TYPE_CHECKING:
        _connection: sqlite3.Connection
        _lock: RLock

        def _transaction(self) -> AbstractContextManager[None]: ...
        def _insert_transition(self, transition: Any) -> None: ...
        def _save_job_in_transaction(
            self, job: Job, transition: Any, expected: Job
        ) -> None: ...
        def get_job(self, job_id: str) -> Job: ...
        def list_runs(self, job_id: str | None = None) -> list[Any]: ...

    def observe_github_issue(self, issue: SourceIssue, policy: IntakePolicy) -> str:
        if (issue.repository, issue.repository_id) != (
            policy.repository,
            policy.repository_id,
        ):
            raise ValueError("repository is not allowlisted")
        eligible = policy.eligible(issue)
        with self._lock, self._transaction():
            previous = self._connection.execute(
                "SELECT * FROM github_sources WHERE source_key = ?", (issue.key,)
            ).fetchone()
            changed = previous is not None and previous["revision"] != issue.revision
            revoked = changed or not eligible or bool(previous and previous["revoked"])
            self._connection.execute(
                """INSERT INTO github_sources
                (source_key, revision, eligible, revoked, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision,
                eligible=excluded.eligible, revoked=excluded.revoked,
                payload=excluded.payload""",
                (
                    issue.key,
                    issue.revision,
                    eligible,
                    revoked,
                    json.dumps(issue.to_dict()),
                ),
            )
            if previous is None or changed or bool(previous["eligible"]) != eligible:
                self._event(issue, "observed" if eligible else "ineligible")
            job_id = previous["job_id"] if previous else None
            if job_id and revoked:
                job = self.get_job(job_id)
                if job.state in {JobState.BACKLOG, JobState.READY, JobState.PLANNING}:
                    target = JobState.BACKLOG if eligible else JobState.CANCELLED
                    if target != job.state:
                        updated, event = transition_job(
                            job, target, "GitHub source approval revoked"
                        )
                        self._save_job_in_transaction(updated, event, job)
            return (
                "approval_required"
                if revoked or previous is None or not previous["approved_revision"]
                else "approved"
            )

    def approve_github_issue(
        self, issue: SourceIssue, policy: IntakePolicy, *, actor: str
    ) -> None:
        """Trusted local administrative call; never invoked from source text."""
        if not actor.strip() or not policy.eligible(issue):
            raise ValueError(
                "approval requires an eligible source and administrative actor"
            )
        with self._lock, self._transaction():
            row = self._connection.execute(
                "SELECT * FROM github_sources WHERE source_key = ?", (issue.key,)
            ).fetchone()
            if row is None or row["revision"] != issue.revision or not row["eligible"]:
                raise ValueError("observe the exact current issue before approval")
            self._connection.execute(
                """UPDATE github_sources SET approved_revision=?,
                approved_by=?, approved_at=CURRENT_TIMESTAMP, revoked=0
                WHERE source_key=?""",
                (issue.revision, actor, issue.key),
            )
            self._event(issue, "approved", actor)

    def create_github_job(self, issue: SourceIssue, job: Job) -> Job:
        if (
            job.id != issue.job_id
            or job.qos is not QoSClass.SCAVENGER
            or job.state is not JobState.BACKLOG
        ):
            raise ValueError(
                "GitHub job requires deterministic identity and scavenger QoS"
            )
        if job.quota_budget.maximum is None:
            raise ValueError("GitHub job requires a bounded quota")
        with self._lock, self._transaction():
            row = self._connection.execute(
                "SELECT * FROM github_sources WHERE source_key=?", (issue.key,)
            ).fetchone()
            if (
                row is None
                or row["revision"] != issue.revision
                or row["approved_revision"] != issue.revision
                or row["revoked"]
                or not row["eligible"]
            ):
                raise ValueError("source is not currently approved")
            if row["job_id"]:
                existing = self.get_job(row["job_id"])
                if existing.state is not JobState.BACKLOG or self.list_runs(
                    existing.id
                ):
                    return existing
                # A pre-launch edit can replace intent only after explicit reapproval.
                from dataclasses import replace

                updated = replace(job, created_at=existing.created_at)
                self._save_job_in_transaction(updated, None, existing)
                job = updated
            else:
                self._connection.execute(
                    "INSERT INTO jobs(id, project, state, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.project,
                        job.state.value,
                        job.created_at.isoformat(),
                        json.dumps(job.to_dict()),
                    ),
                )
                self._insert_transition(
                    initial_transition(job, reason="approved GitHub source")
                )
                self._connection.execute(
                    "UPDATE github_sources SET job_id=? WHERE source_key=?",
                    (job.id, issue.key),
                )
            ready, event = transition_job(
                job, JobState.READY, "exact GitHub source revision approved"
            )
            self._save_job_in_transaction(ready, event, job)
            self._event(issue, "job_created")
            return ready

    def github_source_for_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM github_sources WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def _event(self, issue: SourceIssue, action: str, actor: str | None = None) -> None:
        self._connection.execute(
            "INSERT INTO github_source_events(source_key, revision, action, actor) "
            "VALUES (?, ?, ?, ?)",
            (issue.key, issue.revision, action, actor),
        )
