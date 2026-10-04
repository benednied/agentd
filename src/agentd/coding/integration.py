"""Human-authorized, exact-head GitHub integration owned by the publisher.

An approval records a grant, never starts execution. Only the publisher checks
the delivered candidate, live source, GitHub Actions, and PR identity before a
compare-and-swap merge. Ambiguous writes reconcile against GitHub's merged PR.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agentd.coding.models import exact_commit
from agentd.domain.enums import JobState, RunOutcome, RunState
from agentd.domain.transitions import transition_job
from agentd.intake.models import SourceIssue
from agentd.intake.workflow import StandingGitHubPolicy, _instant, parse_control
from agentd.state.sqlite import SQLiteStateStore


class IntegrationBlocked(ValueError):
    """Current evidence does not authorize integration."""


def integration_status(store: SQLiteStateStore, job_id: str) -> dict[str, Any] | None:
    """Read integration outcome without constructing a privileged composition."""
    with store._lock:
        present = store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='github_integrations'"
        ).fetchone()
        if present is None:
            return None
        row = store._connection.execute(
            "SELECT * FROM github_integrations WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


class GitHubIntegrationAdapter:
    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        arguments = ["gh", "api", "--method", method, endpoint]
        if payload is not None:
            arguments.extend(("--input", "-"))
        result = subprocess.run(
            arguments,
            input=json.dumps(payload) if payload is not None else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        return json.loads(result.stdout)

    def repository(self, repository: str) -> dict[str, Any]:
        return self._api(f"repos/{repository}")

    def pull_request(self, repository: str, number: int) -> dict[str, Any]:
        return self._api(f"repos/{repository}/pulls/{number}")

    def reviews(self, repository: str, number: int) -> tuple[dict[str, Any], ...]:
        result = []
        for page in range(1, 101):
            items = self._api(
                f"repos/{repository}/pulls/{number}/reviews?per_page=100&page={page}"
            )
            result.extend(items)
            if len(items) < 100:
                return tuple(result)
        raise IntegrationBlocked("GitHub review pagination is incomplete")

    def check_runs(self, repository: str, commit: str) -> tuple[dict[str, Any], ...]:
        result = []
        for page in range(1, 101):
            data = self._api(
                f"repos/{repository}/commits/{commit}/check-runs"
                f"?filter=latest&per_page=100&page={page}"
            )
            result.extend(data["check_runs"])
            if len(result) >= int(data["total_count"]):
                return tuple(result)
            if len(data["check_runs"]) < 100:
                raise IntegrationBlocked("GitHub check pagination is incomplete")
        raise IntegrationBlocked("GitHub check pagination is incomplete")

    def ready(self, node_id: str) -> None:
        query = """mutation($id:ID!) {
          markPullRequestReadyForReview(input:{pullRequestId:$id}) {
            pullRequest { id isDraft }
          }
        }"""
        data = self._api(
            "graphql",
            method="POST",
            payload={"query": query, "variables": {"id": node_id}},
        )
        if data.get("errors"):
            raise IntegrationBlocked("GitHub did not make the approved PR ready")

    def merge(
        self, repository: str, number: int, *, commit: str, method: str
    ) -> dict[str, Any]:
        return self._api(
            f"repos/{repository}/pulls/{number}/merge",
            method="PUT",
            payload={"sha": commit, "merge_method": method},
        )


class GitHubIntegrationReconciler:
    def __init__(
        self,
        store: SQLiteStateStore,
        publications: Any,
        config: dict[str, Any],
        *,
        source_refresh: Callable[[SourceIssue], object],
        adapter: GitHubIntegrationAdapter | None = None,
    ) -> None:
        self.store, self.publications = store, publications
        self.ledger = publications.publisher.store
        self.config = config
        self.profile = config["profile"]
        self.repository = self.profile["repository"]
        self.repository_id = config["repository_id"]
        self.base_branch = config["base_branch"]
        values = config.get("standing_github_policy") or config.get("github_workflow")
        if values is None:
            raise ValueError("GitHub integration requires a trusted standing policy")
        self.policy = StandingGitHubPolicy.from_dict(
            {
                "repository": self.repository,
                "repository_id": self.repository_id,
                **values,
            }
        )
        self.required_checks = tuple(
            config.get("integration", {}).get("required_checks", ())
        )
        self.merge_method = config.get("integration", {}).get("merge_method", "merge")
        if not self.required_checks or any(
            not isinstance(name, str) or not name for name in self.required_checks
        ):
            raise ValueError("integration requires explicit GitHub Actions check names")
        if self.merge_method not in {"merge", "squash", "rebase"}:
            raise ValueError("unsupported administrative merge method")
        self.source_refresh = source_refresh
        self.adapter = adapter or GitHubIntegrationAdapter()
        with store._lock, store._transaction():
            store._connection.execute("""CREATE TABLE IF NOT EXISTS
                github_integration_grants (
                event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                run_id TEXT NOT NULL, head_commit TEXT NOT NULL,
                login TEXT NOT NULL, actor_id INTEGER NOT NULL,
                occurred_at TEXT NOT NULL, kind TEXT NOT NULL)""")
            store._connection.execute("""CREATE TABLE IF NOT EXISTS
                github_integrations (
                job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                head_commit TEXT NOT NULL, pr_number INTEGER NOT NULL,
                stage TEXT NOT NULL, reason TEXT, merge_commit TEXT,
                completed_at TEXT)""")

    def get(self, job_id: str) -> dict[str, Any] | None:
        return integration_status(self.store, job_id)

    def _candidate(self, job_id: str) -> tuple[dict[str, Any], Any, Any, SourceIssue]:
        publication = self.ledger.get(job_id)
        job = self.store.get_job(job_id)
        run = self.store.latest_run(job_id)
        if (
            not publication
            or publication["stage"] != "published"
            or not publication.get("delivery")
            or not publication.get("pr")
            or run is None
            or run.state is not RunState.COMPLETED
            or run.result is None
            or run.result.outcome is not RunOutcome.COMPLETED
            or job.state not in {JobState.REVIEW, JobState.COMPLETED}
            or self.store.find_active_run(job_id) is not None
        ):
            raise IntegrationBlocked("candidate is not a quiescent completed delivery")
        intent, delivery = publication["intent"], publication["delivery"]
        if (
            intent["run_id"] != run.id
            or delivery["run_id"] != run.id
            or intent["result_commit"] != run.result.commit
            or delivery["result_commit"] != run.result.commit
            or not delivery.get("completed_at")
        ):
            raise IntegrationBlocked(
                "latest metered run does not match delivered candidate"
            )
        if (
            not isinstance(run.result.metadata.get("coding_evidence"), dict)
            or run.result.metadata.get("telemetry_valid") is not True
            or run.result.usage is None
            or self.store.find_active_reservation(job_id) is not None
            or self.store.find_active_allocation(job_id) is not None
        ):
            raise IntegrationBlocked(
                "delivery lacks valid terminal ownership and usage"
            )
        source = self.store.github_source_for_job(job_id)
        if (
            source is None
            or source["revoked"]
            or not source["eligible"]
            or source["revision"] != intent["source_revision"]
            or source["approved_revision"] != intent["source_revision"]
        ):
            raise IntegrationBlocked("source authorization changed before integration")
        issue = SourceIssue.from_dict(json.loads(source["payload"]))
        if (issue.repository, issue.repository_id) != (
            self.repository,
            self.repository_id,
        ):
            raise IntegrationBlocked("integration source repository identity changed")
        return publication, job, run, issue

    def _hold_and_feedback(
        self,
        job_id: str,
        issue_number: int,
        pr_number: int,
        *,
        excluding: str | None = None,
    ) -> None:
        if self.store.github_job_held(job_id):
            raise IntegrationBlocked("job is paused by GitHub control")
        for event in self.store.github_controls("pending"):
            if event["event_id"] == excluding or event["issue_number"] not in {
                issue_number,
                pr_number,
            }:
                continue
            action, _ = parse_control(event["payload"]["body"])
            if action not in {"ignore", "approve"}:
                raise IntegrationBlocked("trusted GitHub feedback remains pending")

    @staticmethod
    def _number(publication: dict[str, Any]) -> int:
        url = publication["pr"]["url"]
        if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*", url):
            raise IntegrationBlocked("published PR URL identity is invalid")
        return int(url.rsplit("/", 1)[1])

    async def approve_pr(
        self,
        job_id: str,
        *,
        actor: str,
        event_id: str,
        occurred_at: str,
        head_commit: str | None = None,
    ) -> None:
        """Record authenticated comment approval without network or worker calls."""
        match = re.fullmatch(r"github:([A-Za-z0-9-]+):([1-9][0-9]*)", actor)
        if not match or not self.policy.trusts(match[1], int(match[2])):
            raise IntegrationBlocked("PR approval actor is not trusted")
        publication, _job, run, issue = self._candidate(job_id)
        number = self._number(publication)
        self._hold_and_feedback(job_id, issue.number, number, excluding=event_id)
        commit = publication["intent"]["result_commit"]
        if head_commit is not None and exact_commit(head_commit) != commit:
            raise IntegrationBlocked("approval does not match the delivered head")
        if _instant(occurred_at) < _instant(publication["delivery"]["completed_at"]):
            raise IntegrationBlocked(
                "approval predates the current delivered candidate"
            )
        self._grant(
            event_id,
            job_id,
            run.id,
            commit,
            match[1],
            int(match[2]),
            occurred_at,
            "comment",
        )

    def _grant(
        self,
        event_id: str,
        job_id: str,
        run_id: str,
        commit: str,
        login: str,
        actor_id: int,
        occurred_at: str,
        kind: str,
    ) -> None:
        if not event_id:
            raise IntegrationBlocked("approval requires a durable event identity")
        values = (
            event_id,
            job_id,
            run_id,
            commit,
            login.lower(),
            actor_id,
            occurred_at,
            kind,
        )
        with self.store._lock, self.store._transaction():
            previous = self.store._connection.execute(
                "SELECT * FROM github_integration_grants WHERE event_id=?", (event_id,)
            ).fetchone()
            if previous is not None and tuple(previous) != values:
                raise IntegrationBlocked(
                    "approval event cannot be rebound to another candidate"
                )
            self.store._connection.execute(
                "INSERT OR IGNORE INTO github_integration_grants "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def _approved(
        self, publication: dict[str, Any], reviews: tuple[dict[str, Any], ...]
    ) -> bool:
        intent, delivery = publication["intent"], publication["delivery"]
        latest = {}
        for review in reviews:
            actor = review.get("user") or {}
            if not self.policy.trusts(
                actor.get("login"), actor.get("id")
            ) or not review.get("submitted_at"):
                continue
            key = actor["id"]
            if key not in latest or int(review["id"]) > int(latest[key]["id"]):
                latest[key] = review
        native_events = set()
        negative_at = None
        for review in latest.values():
            if review["state"] == "CHANGES_REQUESTED":
                when = _instant(review["submitted_at"])
                negative_at = max(negative_at, when) if negative_at else when
            if (
                review["state"] != "APPROVED"
                or review.get("commit_id") != intent["result_commit"]
                or _instant(review["submitted_at"]) < _instant(delivery["completed_at"])
            ):
                continue
            event_id = f"github:{self.repository_id}:review-approval:{review['id']}"
            native_events.add(event_id)
            self._grant(
                event_id,
                intent["job_id"],
                intent["run_id"],
                intent["result_commit"],
                review["user"]["login"],
                review["user"]["id"],
                review["submitted_at"],
                "review",
            )
        with self.store._lock:
            rows = self.store._connection.execute(
                "SELECT * FROM github_integration_grants "
                "WHERE job_id=? AND run_id=? AND head_commit=?",
                (intent["job_id"], intent["run_id"], intent["result_commit"]),
            ).fetchall()
        return any(
            self.policy.trusts(row["login"], row["actor_id"])
            and (row["kind"] != "review" or row["event_id"] in native_events)
            and (negative_at is None or _instant(row["occurred_at"]) > negative_at)
            for row in rows
        )

    def _checks(self, commit: str) -> None:
        runs = self.adapter.check_runs(self.repository, commit)
        for name in self.required_checks:
            matching = [item for item in runs if item["name"] == name]
            if not matching or any(
                item.get("head_sha") != commit
                or item.get("app", {}).get("slug") != "github-actions"
                or item.get("status") != "completed"
                or item.get("conclusion") != "success"
                for item in matching
            ):
                raise IntegrationBlocked(
                    f"required GitHub Actions check {name!r} "
                    "is not successful on the exact head"
                )

    def _verify_pr(self, pr: dict[str, Any], publication: dict[str, Any]) -> None:
        intent = publication["intent"]
        if (
            int(pr["number"]) != self._number(publication)
            or pr["head"]["sha"] != intent["result_commit"]
            or pr["head"]["repo"]["id"] != self.repository_id
            or pr["base"]["repo"]["id"] != self.repository_id
            or pr["base"]["ref"] != self.base_branch
            or pr.get("html_url") != publication["pr"]["url"]
        ):
            raise IntegrationBlocked(
                "live PR does not match the delivered repository, base, and head"
            )

    def _record(
        self,
        publication: dict[str, Any],
        stage: str,
        reason: str | None = None,
        merge_commit: str | None = None,
    ) -> None:
        intent = publication["intent"]
        with self.store._lock, self.store._transaction():
            self.store._connection.execute(
                "INSERT INTO github_integrations VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET run_id=excluded.run_id, "
                "head_commit=excluded.head_commit, pr_number=excluded.pr_number, "
                "stage=excluded.stage, reason=excluded.reason, "
                "merge_commit=excluded.merge_commit, "
                "completed_at=excluded.completed_at",
                (
                    intent["job_id"],
                    intent["run_id"],
                    intent["result_commit"],
                    self._number(publication),
                    stage,
                    reason,
                    merge_commit,
                    datetime.now(UTC).isoformat() if stage == "merged" else None,
                ),
            )

    async def reconcile(self) -> tuple[dict[str, Any], ...]:
        if not self.config.get("integration", {}).get("enabled", False):
            return ()
        reports = []
        for job in self.store.list_jobs():
            publication = self.ledger.get(job.id)
            if publication is None or publication["stage"] != "published":
                continue
            if publication["intent"]["repository"] != self.repository:
                continue
            old = self.get(job.id)
            if old and old["stage"] == "merged":
                continue
            try:
                reports.append(await asyncio.to_thread(self._reconcile, job.id))
            except Exception as error:
                reason = (
                    str(error)
                    if isinstance(error, IntegrationBlocked)
                    else type(error).__name__
                )
                current = self.get(job.id)
                stage = (
                    "merge_pending"
                    if current and current["stage"] == "merge_pending"
                    else "blocked"
                )
                self._record(publication, stage, reason)
                reports.append(
                    {
                        "job_id": job.id,
                        "integration_stage": stage,
                        "integration_wait_reason": reason,
                    }
                )
        return tuple(reports)

    def _reconcile(self, job_id: str) -> dict[str, Any]:
        publication = self.ledger.get(job_id)
        assert publication is not None
        number = self._number(publication)
        if self.adapter.repository(self.repository)["id"] != self.repository_id:
            raise IntegrationBlocked("GitHub repository identity changed")
        pr = self.adapter.pull_request(self.repository, number)
        self._verify_pr(pr, publication)
        if pr.get("merged"):
            # Merging can close the issue before a lost-response reconciliation.
            # Read-only recognition of an exact delivered merge does not require
            # continuing issue authority, and must not launch another run.
            job = self.store.get_job(job_id)
            run = self.store.latest_run(job_id)
            intent, delivery = publication["intent"], publication.get("delivery")
            if (
                not delivery
                or run is None
                or run.id != intent["run_id"]
                or run.id != delivery["run_id"]
                or run.result is None
                or run.result.commit != intent["result_commit"]
                or self.store.find_active_run(job_id) is not None
                or self.store.find_active_reservation(job_id) is not None
                or self.store.find_active_allocation(job_id) is not None
                or job.state
                not in {JobState.REVIEW, JobState.COMPLETED, JobState.CANCELLED}
            ):
                raise IntegrationBlocked(
                    "merged delivery has unresolved local ownership"
                )
            return self._complete(publication, job, pr)
        publication, job, run, issue = self._candidate(job_id)
        if pr["state"] != "open":
            raise IntegrationBlocked("delivered PR was closed without integration")
        self._hold_and_feedback(job_id, issue.number, number)
        if not self._approved(
            publication, self.adapter.reviews(self.repository, number)
        ):
            raise IntegrationBlocked(
                "waiting for trusted human approval of the delivered head"
            )
        self._checks(run.result.commit)
        self.source_refresh(issue)
        # Refresh ledger and source after network checks, so pending feedback,
        # source revocation, or a new candidate cannot reuse the prior grant.
        refreshed, current_job, current_run, current_issue = self._candidate(job_id)
        if refreshed["intent"] != publication["intent"] or current_run != run:
            raise IntegrationBlocked("candidate changed during integration checks")
        self._hold_and_feedback(job_id, current_issue.number, number)
        if pr.get("draft"):
            self.adapter.ready(pr["node_id"])
        pr = self.adapter.pull_request(self.repository, number)
        self._verify_pr(pr, publication)
        if pr.get("merged"):
            return self._complete(publication, current_job, pr)
        if pr["state"] != "open" or pr.get("draft") or pr.get("mergeable") is not True:
            raise IntegrationBlocked("approved PR is not ready and mergeable")
        self._record(publication, "merge_pending")
        result = self.adapter.merge(
            self.repository, number, commit=run.result.commit, method=self.merge_method
        )
        if not result.get("merged"):
            raise IntegrationBlocked("GitHub did not merge the expected head")
        observed = self.adapter.pull_request(self.repository, number)
        self._verify_pr(observed, publication)
        if not observed.get("merged"):
            raise IntegrationBlocked("successful merge response is not yet visible")
        return self._complete(publication, current_job, observed)

    def _complete(
        self, publication: dict[str, Any], job: Any, pr: dict[str, Any]
    ) -> dict[str, Any]:
        commit = pr.get("merge_commit_sha")
        if not commit:
            raise IntegrationBlocked("merged PR lacks an immutable merge commit")
        exact_commit(commit)
        merged_by = pr.get("merged_by") or {}
        if not self.policy.trusts(merged_by.get("login"), merged_by.get("id")):
            raise IntegrationBlocked(
                "merged candidate lacks a trusted integration actor"
            )
        if job.state is JobState.REVIEW:
            completed, event = transition_job(
                job, JobState.COMPLETED, f"trusted GitHub integration {commit}"
            )
            self.store.save_job(completed, event, expected=job)
        self._record(publication, "merged", merge_commit=commit)
        return {
            "job_id": job.id,
            "integration_stage": "merged",
            "merge_commit": commit,
            "pr": publication["pr"]["url"],
        }
