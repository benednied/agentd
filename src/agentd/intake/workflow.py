"""Authenticated GitHub controls under a fixed administrative standing policy.

GitHub supplies identities; issue and comment bodies supply task intent only.
Neither can select a profile, credential, command, or spending limit.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from agentd.coding.models import fingerprint, repository_name
from agentd.domain.enums import JobState
from agentd.domain.models import CodingOperation, RunCommand
from agentd.intake.github import GitHubIssueSource
from agentd.intake.models import SourceIssue

if TYPE_CHECKING:
    from agentd.intake.service import GitHubIntake
    from agentd.state.sqlite import SQLiteStateStore


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("GitHub workflow timestamps require a timezone")
    return result


@dataclass(frozen=True)
class StandingGitHubPolicy:
    repository: str
    repository_id: int
    trusted_actors: Mapping[str, int]
    activated_at: str

    def __post_init__(self) -> None:
        if (
            self.repository != repository_name(self.repository)
            or self.repository_id <= 0
            or not self.trusted_actors
            or any(
                not re.fullmatch(r"[A-Za-z0-9-]+", login)
                or not isinstance(identity, int)
                or isinstance(identity, bool)
                or identity <= 0
                for login, identity in self.trusted_actors.items()
            )
        ):
            raise ValueError("standing policy requires immutable trusted GitHub actors")
        _instant(self.activated_at)

    def trusts(self, login: str | None, identity: int | None) -> bool:
        return bool(
            login and identity and self.trusted_actors.get(login.lower()) == identity
        )

    @property
    def digest(self) -> str:
        return fingerprint(asdict(self))

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> StandingGitHubPolicy:
        return cls(
            repository=values["repository"],
            repository_id=values["repository_id"],
            trusted_actors={
                login.lower(): identity
                for login, identity in values["trusted_actors"].items()
            },
            activated_at=values["activated_at"],
        )


@dataclass(frozen=True)
class GitHubComment:
    node_id: str
    identity: int
    subject_number: int
    author_login: str
    author_id: int
    body: str
    created_at: str
    updated_at: str
    kind: str = "comment"

    def event_id(self, repository_id: int) -> str:
        return f"github:{repository_id}:{self.kind}:{self.identity}"


class GitHubWorkflowSource(GitHubIssueSource):
    """Read APIs with exact body/title and editor provenance checks."""

    _actor = "login ... on User { databaseId }"

    def _graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        completed = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": variables}),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = json.loads(completed.stdout)
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise ValueError("GitHub provenance response is incomplete")
        return result["data"]

    def issue_authority(
        self, issue: SourceIssue
    ) -> tuple[SourceIssue, tuple[tuple[str | None, int | None], ...]]:
        owner, name = issue.repository.split("/")
        query = """query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { databaseId
            issue(number:$number) { id title body createdAt lastEditedAt
              author { ACTOR } editor { ACTOR }
              timelineItems(last:1, itemTypes:[RENAMED_TITLE_EVENT]) {
                nodes { ... on RenamedTitleEvent {
                  createdAt currentTitle actor { ACTOR }
                } }
              }
            }
          }
        }""".replace("ACTOR", self._actor)
        repo = self._graphql(query, owner=owner, name=name, number=issue.number)[
            "repository"
        ]
        node = repo["issue"]
        if (
            int(repo["databaseId"]) != issue.repository_id
            or not node
            or (node["id"], node["title"], node["body"] or "")
            != (issue.node_id, issue.title, issue.body)
        ):
            raise ValueError("GitHub issue changed during provenance verification")
        author = node.get("author") or {}
        editor = node.get("editor") or author
        actors = [
            (author.get("login"), author.get("databaseId")),
            (editor.get("login"), editor.get("databaseId")),
        ]
        times = [node["createdAt"]]
        if node.get("lastEditedAt"):
            times.append(node["lastEditedAt"])
            if not node.get("editor"):
                raise ValueError("edited issue lacks authenticated editor provenance")
        renames = node["timelineItems"]["nodes"]
        if renames:
            latest = renames[-1]
            if latest["currentTitle"] != issue.title:
                raise ValueError("GitHub title edit provenance is incomplete")
            actor = latest.get("actor") or {}
            actors.append((actor.get("login"), actor.get("databaseId")))
            times.append(latest["createdAt"])
        return (
            replace(
                issue,
                author_login=author.get("login"),
                author_id=author.get("databaseId"),
                created_at=node["createdAt"],
                material_updated_at=max(times, key=_instant),
            ),
            tuple(actors),
        )

    def get(self, repository: str, number: int) -> SourceIssue:
        issue = super().get(repository, number)
        if issue.is_pull_request:
            return issue
        return self.issue_authority(issue)[0]

    def comments(
        self, repository: str, *, since: str, kind: str
    ) -> tuple[GitHubComment, ...]:
        repository = repository_name(repository)
        endpoint = "issues/comments" if kind == "comment" else "pulls/comments"
        result = []
        for page in range(1, 101):
            items = self._get(
                f"repos/{repository}/{endpoint}?"
                + urlencode({"since": since, "per_page": 100, "page": page})
            )
            for item in items:
                subject = item.get("issue_url") or item.get("pull_request_url")
                if not subject:
                    raise ValueError("GitHub comment lacks a subject identity")
                author = item.get("user") or {}
                result.append(
                    GitHubComment(
                        node_id=item["node_id"],
                        identity=int(item["id"]),
                        subject_number=int(subject.rsplit("/", 1)[1]),
                        author_login=author.get("login", ""),
                        author_id=author.get("id", 0),
                        body=item.get("body") or "",
                        created_at=item["created_at"],
                        updated_at=item["updated_at"],
                        kind=kind,
                    )
                )
            if len(items) < 100:
                return tuple(result)
        raise ValueError("GitHub control comment pagination is incomplete")

    def verify_comment(self, comment: GitHubComment) -> tuple[tuple[str, int], ...]:
        typename = {
            "comment": "IssueComment",
            "review-comment": "PullRequestReviewComment",
            "review": "PullRequestReview",
        }[comment.kind]
        query = """query($id:ID!) { node(id:$id) { ... on TYPE {
          id body author { ACTOR } editor { ACTOR } lastEditedAt
        } } }""".replace("TYPE", typename).replace("ACTOR", self._actor)
        node = self._graphql(query, id=comment.node_id).get("node")
        if not node or (node["id"], node["body"]) != (
            comment.node_id,
            comment.body,
        ):
            raise ValueError("GitHub control comment changed during verification")
        if node.get("lastEditedAt") and not node.get("editor"):
            raise ValueError("edited comment lacks authenticated editor provenance")
        author = node.get("author") or {}
        editor = node.get("editor") or author
        if (author.get("login"), author.get("databaseId")) != (
            comment.author_login,
            comment.author_id,
        ):
            raise ValueError("GitHub control author identity changed")
        return tuple(
            (actor.get("login", ""), actor.get("databaseId", 0))
            for actor in (author, editor)
        )

    def reviews(self, repository: str, number: int) -> tuple[GitHubComment, ...]:
        result = []
        for page in range(1, 101):
            values = self._get(
                f"repos/{repository}/pulls/{number}/reviews?per_page=100&page={page}"
            )
            for item in values:
                body = item.get("body") or ""
                if item["state"] == "APPROVED" and not body.startswith("/agentd"):
                    continue
                if item["state"] not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}:
                    continue
                author = item.get("user") or {}
                result.append(
                    GitHubComment(
                        item["node_id"],
                        int(item["id"]),
                        number,
                        author.get("login", ""),
                        author.get("id", 0),
                        body,
                        item["submitted_at"],
                        item["submitted_at"],
                        kind="review",
                    )
                )
            if len(values) < 100:
                return tuple(result)
        raise ValueError("GitHub review control pagination is incomplete")


def parse_control(body: str) -> tuple[str, str]:
    body = body.strip()
    if (
        not body
        or "<!-- agentd-status:" in body
        or "<!-- agentd:selfhost-release-status:v1 -->" in body
        or "<!-- agentd:host-abandon:" in body
        or len(body) > 20000
    ):
        return "ignore", ""
    if not body.startswith("/agentd"):
        return "steer", body
    match = re.fullmatch(
        r"/agentd (approve|pause|cancel|resume|retry|steer|abandon)(?:\s+(.+))?",
        body,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(
            "unknown /agentd control; use approve, pause, cancel, "
            "resume, retry, steer, or abandon"
        )
    action, argument = match.group(1), (match.group(2) or "").strip()
    if action in {"pause", "cancel", "resume"} and argument:
        raise ValueError("this control does not accept configuration arguments")
    if (
        action == "approve"
        and argument
        and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", argument)
    ):
        raise ValueError("approval accepts only the exact source revision")
    if action == "steer" and not argument:
        raise ValueError("steering requires task instructions")
    return action, argument


class GitHubWorkflow:
    def __init__(
        self,
        intake: GitHubIntake,
        source: GitHubWorkflowSource,
        policy: StandingGitHubPolicy,
        *,
        feedback: Callable[..., Awaitable[object]] | None = None,
        resume_guard: Callable[[str], None] | None = None,
        approve_pr: Callable[..., Awaitable[object]] | None = None,
        published_subjects: Callable[[], Mapping[int, str]] | None = None,
    ) -> None:
        if (policy.repository, policy.repository_id) != (
            intake.policies[policy.repository].repository,
            intake.policies[policy.repository].repository_id,
        ):
            raise ValueError("standing policy repository does not match intake")
        self.intake, self.source, self.policy = intake, source, policy
        self.feedback = feedback
        self.resume_guard = resume_guard
        self.approve_pr = approve_pr
        self.published_subjects = published_subjects or (lambda: {})

    async def prepare_issue(self, issue: SourceIssue) -> SourceIssue:
        eligibility = self.intake.policies[self.policy.repository]
        if not eligibility.eligible(issue):
            return issue
        row = self.intake.store.github_source(issue.key)
        if row and row["approved_revision"] == issue.revision and not row["revoked"]:
            approved_by = row["approved_by"] or ""
            actor = re.fullmatch(
                r"github-policy:[0-9a-f]{64}:([A-Za-z0-9-]+):([1-9][0-9]*)",
                approved_by,
            )
            trusted = actor is None or self.policy.trusts(actor[1], int(actor[2]))
            if approved_by.startswith("github-comment:"):
                event_id = approved_by.removeprefix("github-comment:").rsplit(":", 1)[0]
                event = self.intake.store.github_control(event_id)
                payload = json.loads(event["payload"]) if event else {}
                trusted = self.policy.trusts(
                    payload.get("author_login"), payload.get("author_id")
                )
            if trusted:
                # Exact unchanged material already has authenticated provenance.
                # Dispatch still observes fresh issue state/eligibility, while
                # publisher refresh independently checks live editor metadata.
                return issue
            self.intake.store.revoke_github_source(issue, actor="standing-policy")
            return issue
        if (
            not self.policy.trusts(issue.author_login, issue.author_id)
            or issue.created_at is None
            or _instant(issue.created_at) < _instant(self.policy.activated_at)
        ):
            return issue
        verified, actors = await asyncio.to_thread(self.source.issue_authority, issue)
        self.intake.store.observe_github_issue(verified, eligibility)
        row = self.intake.store.github_source(verified.key)
        if row and row["approved_revision"] == verified.revision and not row["revoked"]:
            return verified
        if (
            verified.created_at is None
            or _instant(verified.created_at) < _instant(self.policy.activated_at)
            or not all(self.policy.trusts(*actor) for actor in actors)
        ):
            return verified
        # An executed work order, checkpoint, or result must never be silently
        # rebound to newly edited source intent. Comments carry its follow-up.
        if self.intake.store.list_runs(verified.job_id):
            return verified
        self.intake.store.approve_github_issue(
            verified,
            eligibility,
            actor=(
                f"github-policy:{self.policy.digest}:"
                f"{verified.author_login}:{verified.author_id}"
            ),
        )
        return verified

    async def poll_controls(self) -> None:
        store = self.intake.store
        for kind in ("comment", "review-comment"):
            stream = f"{self.policy.repository_id}:{kind}"
            cursor = store.github_control_cursor(stream, self.policy.activated_at)
            since = (
                (_instant(cursor) - timedelta(seconds=1)).astimezone(UTC).isoformat()
            )
            comments = await asyncio.to_thread(
                self.source.comments,
                self.policy.repository,
                since=since,
                kind=kind,
            )
            for comment in sorted(
                comments, key=lambda value: (_instant(value.created_at), value.identity)
            ):
                event_id = comment.event_id(self.policy.repository_id)
                if store.github_control(event_id) is not None:
                    continue
                if _instant(comment.created_at) < _instant(
                    self.policy.activated_at
                ) or not self.policy.trusts(comment.author_login, comment.author_id):
                    continue
                actors = await asyncio.to_thread(self.source.verify_comment, comment)
                if not all(self.policy.trusts(*actor) for actor in actors):
                    continue
                store.record_github_control(
                    event_id,
                    comment.subject_number,
                    asdict(comment),
                )
            if comments:
                store.advance_github_control_cursor(
                    stream, max((item.updated_at for item in comments), key=_instant)
                )
        for number in self.published_subjects():
            reviews = await asyncio.to_thread(
                self.source.reviews, self.policy.repository, number
            )
            for review in reviews:
                event_id = review.event_id(self.policy.repository_id)
                if (
                    store.github_control(event_id) is not None
                    or not self.policy.trusts(review.author_login, review.author_id)
                    or _instant(review.created_at) < _instant(self.policy.activated_at)
                ):
                    continue
                actors = await asyncio.to_thread(self.source.verify_comment, review)
                if all(self.policy.trusts(*actor) for actor in actors):
                    store.record_github_control(event_id, number, asdict(review))
        await self.apply_pending()

    def _issue_for_subject(self, number: int) -> SourceIssue:
        job_id = self.published_subjects().get(number)
        if job_id:
            row = self.intake.store.github_source_for_job(job_id)
            if row is None:
                raise ValueError("published PR lacks source provenance")
            previous = SourceIssue.from_dict(json.loads(row["payload"]))
            return self.source.get(previous.repository, previous.number)
        issue = self.source.get(self.policy.repository, number)
        if issue.is_pull_request:
            raise ValueError("PR does not belong to an agentd publication")
        return issue

    async def apply_pending(self) -> None:
        store = self.intake.store
        for event in store.github_controls("pending"):
            if not event["event_id"].startswith(f"github:{self.policy.repository_id}:"):
                continue
            job_id = None
            try:
                payload = event["payload"]
                if not self.policy.trusts(
                    payload["author_login"], payload["author_id"]
                ):
                    raise ValueError("control actor is no longer trusted")
                actors = await asyncio.to_thread(
                    self.source.verify_comment, GitHubComment(**payload)
                )
                if not all(self.policy.trusts(*actor) for actor in actors):
                    raise ValueError("control editor is no longer trusted")
                action, instruction = parse_control(event["payload"]["body"])
                if action in {"ignore", "abandon"}:
                    store.finish_github_control(
                        event["event_id"],
                        job_id=None,
                        state="ignored",
                        reason="handled by the trusted HP supervisor"
                        if action == "abandon"
                        else None,
                    )
                    continue
                issue = await asyncio.to_thread(
                    self._issue_for_subject, event["issue_number"]
                )
                job_id = issue.job_id
                if action == "approve":
                    if event["issue_number"] in self.published_subjects():
                        if self.approve_pr is None:
                            raise ValueError("PR integration is unavailable")
                        await self.approve_pr(
                            job_id,
                            actor=(
                                f"github:{payload['author_login']}:"
                                f"{payload['author_id']}"
                            ),
                            event_id=event["event_id"],
                            occurred_at=payload["created_at"],
                            head_commit=instruction or None,
                        )
                    else:
                        await self._approve(issue, event, instruction)
                else:
                    try:
                        job = store.get_job(job_id)
                    except LookupError:
                        await self.intake.reconcile(issue)
                        try:
                            job = store.get_job(job_id)
                        except LookupError as error:
                            raise ValueError(
                                "This issue is outside automatic intake; comment "
                                "/agentd approve after reviewing its current contents."
                            ) from error
                    if action == "pause":
                        store.set_github_job_hold(
                            job_id, held=True, event_id=event["event_id"]
                        )
                        active = store.find_active_run(job_id)
                        if active:
                            store.enqueue_run_command(
                                RunCommand(
                                    id=event["event_id"],
                                    run_id=active.id,
                                    action="interrupt",
                                )
                            )
                    elif action == "cancel":
                        store.set_github_job_hold(
                            job_id, held=True, event_id=event["event_id"]
                        )
                        if not job.terminal:
                            await self.intake.control_plane.cancel(job_id)
                    elif action == "resume":
                        await asyncio.to_thread(
                            self.intake.refresh_authorization, issue
                        )
                        if job.state is JobState.SUSPENDED and self.resume_guard:
                            self.resume_guard(job_id)
                        store.set_github_job_hold(
                            job_id, held=False, event_id=event["event_id"]
                        )
                        if job.state is JobState.SUSPENDED:
                            await self.intake.control_plane.resume(job_id)
                        elif job.state not in {
                            JobState.READY,
                            JobState.RUNNING,
                            JobState.REVIEW,
                        }:
                            raise ValueError("job cannot resume from its current state")
                    else:
                        if not instruction:
                            instruction = (
                                "Retry the previous task and resolve "
                                "its recorded failures."
                            )
                        if store.github_job_held(job_id):
                            raise ValueError(
                                "job is paused; comment /agentd resume first"
                            )
                        if store.find_active_run(job_id) is not None:
                            # Remote coding rejects out-of-order intent. Preserve
                            # the event until trusted terminal collection allows a
                            # fresh bounded continuation, even across restart.
                            continue
                        if (
                            not store.list_runs(job_id)
                            and job.state is JobState.BACKLOG
                        ):
                            continue
                        if not store.list_runs(job_id) and job.state is JobState.READY:
                            self._append_queued_intent(job, event, instruction)
                        elif self.feedback is not None:
                            await self.feedback(
                                job_id,
                                instruction,
                                actor=f"github:{event['payload']['author_login']}:{event['payload']['author_id']}",
                                event_id=event["event_id"],
                            )
                        else:
                            raise ValueError(
                                "bounded coding continuation is unavailable"
                            )
                store.finish_github_control(
                    event["event_id"], job_id=job_id, state="applied"
                )
                if job_id:
                    store.resolve_github_control_blocks(job_id, event["event_id"])
            except (LookupError, ValueError, RuntimeError) as error:
                store.finish_github_control(
                    event["event_id"], job_id=job_id, state="blocked", reason=str(error)
                )
                if job_id is None or store.github_source_for_job(job_id) is None:
                    key = (
                        f"github-control:{self.policy.repository_id}:"
                        f"{event['issue_number']}"
                    )
                    self.intake.store.queue_github_status(
                        key,
                        self.policy.repository,
                        self.policy.repository_id,
                        event["issue_number"],
                        f"<!-- agentd-status:{fingerprint(key)} -->\n\n"
                        f"agentd could not apply your comment: {error}",
                    )

    async def _approve(
        self, issue: SourceIssue, event: dict[str, Any], revision: str
    ) -> None:
        policy = self.intake.policies[self.policy.repository]
        if revision:
            if revision != issue.revision:
                raise ValueError("approval revision no longer matches the issue")
        elif not issue.material_updated_at or _instant(
            issue.material_updated_at
        ) > _instant(event["payload"]["created_at"]):
            raise ValueError(
                "issue changed after the approval comment; approve its current revision"
            )
        if self.intake.store.list_runs(issue.job_id):
            row = self.intake.store.github_source_for_job(issue.job_id)
            if row and row["approved_revision"] != issue.revision:
                raise ValueError(
                    "executed issue intent changed; "
                    "post a new issue for the revised task"
                )
        self.intake.store.observe_github_issue(issue, policy)
        self.intake.store.approve_github_issue(
            issue,
            policy,
            actor=f"github-comment:{event['event_id']}:{event['payload']['author_id']}",
        )
        await self.intake.reconcile(issue)

    def _append_queued_intent(
        self, job: Any, event: dict[str, Any], instruction: str
    ) -> None:
        marker = f"GitHub feedback {event['event_id']}"
        if marker in job.objective:
            return
        objective = job.objective + f"\n\n{marker}:\n{instruction}"
        operation = job.operation
        if isinstance(operation, CodingOperation):
            operation = CodingOperation(
                replace(operation.work_order, objective=objective)
            )
        updated = replace(job, objective=objective, operation=operation)
        self.intake.store.save_job(updated, None, expected=job)


class GitHubStatusRejected(RuntimeError):
    """The API explicitly rejected a write without accepting the side effect."""


class GitHubStatusAdapter(GitHubIssueSource):
    """Publisher-owned write adapter, separate from intake credentials."""

    def __init__(self) -> None:
        self._publisher_id: int | None = None

    def _write(self, method: str, endpoint: str, body: str) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["gh", "api", "--method", method, endpoint, "--input", "-"],
                input=json.dumps({"body": body}),
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as error:
            rejected = re.search(
                r"HTTP (400|401|403|404|410|422|429)", error.stderr or ""
            )
            if rejected:
                raise GitHubStatusRejected(
                    f"GitHub rejected status write (HTTP {rejected[1]})"
                ) from error
            raise
        return json.loads(completed.stdout)

    def verify_repository(self, repository: str, repository_id: int) -> None:
        if int(self._get(f"repos/{repository}")["id"]) != repository_id:
            raise ValueError("GitHub status repository identity changed")

    def find_status(
        self, repository: str, number: int, marker: str
    ) -> dict[str, Any] | None:
        if self._publisher_id is None:
            self._publisher_id = int(self._get("user")["id"])
        found = []
        for page in range(1, 101):
            values = self._get(
                f"repos/{repository}/issues/{number}/comments?per_page=100&page={page}"
            )
            found.extend(
                value
                for value in values
                if marker in (value.get("body") or "")
                and value.get("user", {}).get("id") == self._publisher_id
            )
            if len(values) < 100:
                if len(found) > 1:
                    raise ValueError(
                        "multiple GitHub status comments have the same identity"
                    )
                return found[0] if found else None
        raise ValueError("GitHub status comment pagination is incomplete")

    def create(self, repository: str, number: int, body: str) -> dict[str, Any]:
        return self._write("POST", f"repos/{repository}/issues/{number}/comments", body)

    def update(self, repository: str, identity: int, body: str) -> dict[str, Any]:
        return self._write(
            "PATCH", f"repos/{repository}/issues/comments/{identity}", body
        )


class GitHubStatusReporter:
    def __init__(
        self,
        store: SQLiteStateStore,
        adapter: GitHubStatusAdapter,
        *,
        repository: str | None = None,
    ) -> None:
        self.store, self.adapter = store, adapter
        self.repository = repository

    def enqueue(self, reports: list[dict[str, Any]]) -> None:
        for report in reports:
            job_id = report["job_id"]
            row = self.store.github_source_for_job(job_id)
            if row is None:
                continue
            issue = SourceIssue.from_dict(json.loads(row["payload"]))
            if self.repository is not None and issue.repository != self.repository:
                continue
            marker = f"<!-- agentd-status:{fingerprint(job_id)} -->"
            state = report["state"]
            if report.get("publication_stage") == "published":
                state = "Delivered"
            if report.get("integration_stage") == "merged":
                state = "Integrated"
            lines = [
                marker,
                f"agentd: **{state}**",
            ]
            attempts = report.get("attempt_budget")
            if attempts:
                lines.append(
                    f"Attempts: {attempts['total_attempts']} total; "
                    f"{attempts['coding_attempts']} coding of "
                    f"{attempts['maximum_coding_attempts']} allowed; "
                    f"{attempts['preparation_attempts']} preparation failures of "
                    f"{attempts['maximum_preparation_attempts']} allowed. "
                    f"Total limit: {attempts['maximum_total_attempts']}."
                )
            else:
                lines.append(f"Attempts: {report.get('attempts', 0)}.")
            if self.store.github_job_held(job_id):
                lines.append(
                    "Paused by your GitHub control. "
                    "Comment `/agentd resume` to continue."
                )
            if report.get("pr"):
                lines.append(f"Pull request: {report['pr']}")
            reasons = [
                report.get(key)
                for key in (
                    "backlog_wait_reason",
                    "quota_wait_reason",
                    "publication_error",
                    "recovery_error",
                    "integration_wait_reason",
                    "blocked_reason",
                )
            ]
            quota = report.get("quota") or {}
            reasons.append(quota.get("provider_wait_reason"))
            if (
                quota.get("local_available") is not None
                and quota["local_available"] <= 0
            ):
                reasons.append("local spending allowance is waiting for renewal")
            repair = report.get("repair") or {}
            exhausted = repair.get("status") == "exhausted"
            if repair.get("status") == "blocked":
                reasons.append(repair.get("reason"))
            elif exhausted:
                lines.append(
                    f"Stopped: {repair.get('reason') or 'Repair limit reached'}"
                )
            reasons.extend(
                event["reason"]
                for event in self.store.github_controls("blocked")
                if event["job_id"] == job_id and event["reason"]
            )
            lines.extend(
                f"Waiting: {reason}"
                for reason in dict.fromkeys(reason for reason in reasons if reason)
            )
            if (
                not report.get("authorized", True)
                and not self.store.github_job_held(job_id)
                and report.get("integration_stage") != "merged"
            ):
                lines.append(
                    "Source approval was revoked. "
                    "Post a new issue for revised executed intent."
                )
            if exhausted:
                lines.append(
                    "Automatic repair has stopped. `/agentd retry` does not raise "
                    "attempt or spending limits. Post a new issue to authorize "
                    "a separate job, or ask the operator to revise the limits "
                    "before retrying this job. A new job shares the account's "
                    "existing spending allowance."
                )
                lines.append(
                    "Use `/agentd pause` or `/agentd cancel` to stop this job."
                )
            else:
                lines.append(
                    "Comment with instructions, or use `/agentd pause`, "
                    "`/agentd resume`, `/agentd retry`, or `/agentd cancel`."
                )
            self.store.queue_github_status(
                job_id,
                issue.repository,
                issue.repository_id,
                issue.number,
                "\n\n".join(lines),
            )

    async def publish_pending(self) -> tuple[dict[str, Any], ...]:
        results = []
        for report in self.store.pending_github_status():
            if self.repository is not None and report["repository"] != self.repository:
                continue
            try:
                result = await asyncio.to_thread(self._publish, report)
                results.append(result)
            except Exception as error:
                results.append(
                    {
                        "job_id": report["report_key"],
                        "status_error": type(error).__name__,
                    }
                )
        return tuple(results)

    def _publish(self, report: dict[str, Any]) -> dict[str, Any]:
        self.adapter.verify_repository(report["repository"], report["repository_id"])
        marker = f"<!-- agentd-status:{fingerprint(report['report_key'])} -->"
        existing = self.adapter.find_status(
            report["repository"], report["issue_number"], marker
        )
        if existing is None:
            if report["started"]:
                # A lost POST response is ambiguous. Keep reconciling its marker
                # without blindly repeating creation and duplicating messages.
                return {
                    "job_id": report["report_key"],
                    "status_pending": "creation response unresolved",
                }
            self.store.start_github_status(report["report_key"])
            try:
                existing = self.adapter.create(
                    report["repository"], report["issue_number"], report["body"]
                )
            except GitHubStatusRejected:
                self.store.retry_rejected_github_status(report["report_key"])
                raise
        elif existing["body"] != report["body"]:
            existing = self.adapter.update(
                report["repository"], int(existing["id"]), report["body"]
            )
        self.store.finish_github_status(
            report["report_key"], int(existing["id"]), report["body"]
        )
        return {"job_id": report["report_key"], "status_url": existing.get("html_url")}
