"""Fail-closed evidence for integrating a change into a target repository.

This module deliberately treats GitHub observations as evidence, rather than as
instructions.  A pull request is usable only when its identity and checks are
fresh, it is an unambiguous candidate, and the target base demonstrably contains
the requested prerequisites.  External implementations require an explicit
administrative policy and matching evidence.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agentd.coding.models import exact_commit, repository_name


class IntegrationStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    MERGED = "merged"
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    name: str
    status: str
    conclusion: str | None = None

    @property
    def passed(self) -> bool:
        return self.status.lower() in {"completed", "success", "passed"} and (
            self.conclusion is None
            or self.conclusion.lower() in {"success", "neutral", "skipped"}
        )


@dataclass(frozen=True, slots=True)
class PullRequestEvidence:
    repository: str
    number: int
    state: str
    draft: bool
    head_sha: str
    base_sha: str
    base_branch: str
    checks: tuple[CheckEvidence, ...] = ()
    merged: bool = False
    merged_sha: str | None = None
    target_contains_merge: bool | None = None
    target_commits: tuple[str, ...] = ()
    reverted: bool = False
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not self.repository or "/" not in self.repository or self.number <= 0:
            raise ValueError("pull request evidence requires stable identity")
        if self.state not in {"open", "closed"}:
            raise ValueError("pull request state must be open or closed")
        for value in (self.head_sha, self.base_sha):
            try:
                exact_commit(value)
            except ValueError as error:
                raise ValueError(
                    "pull request evidence requires an exact commit"
                ) from error
        if self.observed_at:
            datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))

    @property
    def key(self) -> str:
        return f"github:{self.repository}#{self.number}"

    @property
    def status(self) -> IntegrationStatus:
        if self.merged:
            return IntegrationStatus.MERGED
        if self.draft:
            return IntegrationStatus.DRAFT
        return IntegrationStatus.REVIEW

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PullRequestEvidence:
        checks = tuple(
            c if isinstance(c, CheckEvidence) else CheckEvidence(**c)
            for c in data.get("checks", ())
        )
        values: dict[str, Any] = {
            **data,
            "checks": checks,
            "target_commits": tuple(data.get("target_commits", ())),
        }
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ExternalImplementationEvidence:
    implementation_id: str
    repository: str
    commit: str
    policy_id: str
    policy_digest: str
    prerequisites: tuple[str, ...] = ()
    checks_passed: bool = False
    explicit_approval: bool = False
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not all(
            (
                self.implementation_id,
                self.repository,
                self.commit,
                self.policy_id,
                self.policy_digest,
            )
        ):
            raise ValueError("external implementation evidence is incomplete")
        if self.observed_at:
            datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class IntegrationPolicy:
    repository: str
    base_branch: str
    prerequisites: tuple[str, ...] = ()
    expected_head_sha: str | None = None
    allow_external: bool = False
    external_policy_id: str | None = None
    external_policy_digest: str | None = None
    external_repository: str | None = None
    revoked_pr_numbers: tuple[int, ...] = ()
    max_age_seconds: float = 900


@dataclass(frozen=True, slots=True)
class IntegrationDecision:
    status: IntegrationStatus
    reason: str
    evidence_key: str | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    links: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status is IntegrationStatus.READY


class IntegrationEvidenceEvaluator:
    """Evaluate immutable observations against controller-owned integration policy."""

    def __init__(
        self, policy: IntegrationPolicy, store: IntegrationEvidenceStore | None = None
    ) -> None:
        self.policy = policy
        self.store = store

    def evaluate(
        self,
        pull_requests: Iterable[PullRequestEvidence] = (),
        *,
        external: ExternalImplementationEvidence | None = None,
    ) -> IntegrationDecision:
        prs = tuple(pull_requests)
        if external is not None:
            decision = self._external(external)
        elif not prs:
            decision = IntegrationDecision(
                IntegrationStatus.BLOCKED, "no implementation pull request"
            )
        elif len(prs) != 1:
            decision = IntegrationDecision(
                IntegrationStatus.BLOCKED, "ambiguous pull request evidence"
            )
        else:
            decision = self._pull_request(prs[0])
        if self.store is not None:
            self.store.record(self.policy, decision, prs, external)
        return decision

    def _pull_request(self, pr: PullRequestEvidence) -> IntegrationDecision:
        def blocked(reason: str) -> IntegrationDecision:
            return IntegrationDecision(IntegrationStatus.BLOCKED, reason, pr.key)

        if (
            pr.repository != self.policy.repository
            or pr.base_branch != self.policy.base_branch
        ):
            return blocked("pull request target does not match policy")
        if pr.number in self.policy.revoked_pr_numbers:
            return blocked("pull request was explicitly revoked")
        if self.policy.max_age_seconds > 0:
            if not pr.observed_at:
                return blocked("pull request evidence timestamp is missing")
            try:
                observed = datetime.fromisoformat(pr.observed_at.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    return blocked("pull request evidence timestamp is naive")
                if (observed - datetime.now(UTC)).total_seconds() > 30:
                    return blocked("pull request evidence timestamp is in the future")
                if (
                    datetime.now(UTC) - observed
                ).total_seconds() > self.policy.max_age_seconds:
                    return blocked("pull request evidence is stale")
            except ValueError:
                return blocked("pull request evidence timestamp is invalid")
        if (
            self.policy.expected_head_sha
            and pr.head_sha != self.policy.expected_head_sha
        ):
            return blocked("pull request head changed")
        if pr.reverted:
            return blocked("pull request change was reverted")
        if pr.state == "closed" and not pr.merged:
            return blocked("pull request is closed without merge")
        if pr.draft:
            return blocked("pull request is still a draft")
        if any(not check.passed for check in pr.checks):
            return blocked("pull request has failing checks")
        missing = set(self.policy.prerequisites) - set(pr.target_commits)
        if missing:
            return blocked(
                "target base is missing prerequisites: " + ",".join(sorted(missing))
            )
        if not pr.merged:
            return blocked("pull request is under review")
        if not pr.merged_sha:
            return blocked("merged pull request has no merge commit evidence")
        if pr.target_contains_merge is not True:
            return blocked("target branch does not prove containment of merge commit")
        return IntegrationDecision(
            IntegrationStatus.READY,
            "merged pull request has verified target prerequisites",
            pr.key,
        )

    def _external(
        self, evidence: ExternalImplementationEvidence
    ) -> IntegrationDecision:
        def blocked(reason: str) -> IntegrationDecision:
            return IntegrationDecision(
                IntegrationStatus.BLOCKED, reason, evidence.implementation_id
            )

        if not self.policy.allow_external:
            return blocked("external implementation is not allowed by policy")
        if evidence.repository != (
            self.policy.external_repository or self.policy.repository
        ):
            return blocked("external implementation repository does not match policy")
        if (
            evidence.policy_id != self.policy.external_policy_id
            or evidence.policy_digest != self.policy.external_policy_digest
        ):
            return blocked("external implementation policy evidence does not match")
        if not evidence.explicit_approval:
            return blocked("external implementation lacks explicit approval")
        if not evidence.checks_passed:
            return blocked("external implementation checks failed")
        missing = set(self.policy.prerequisites) - set(evidence.prerequisites)
        if missing:
            return blocked(
                "external implementation is missing prerequisites: "
                + ",".join(sorted(missing))
            )
        return IntegrationDecision(
            IntegrationStatus.READY,
            "explicitly approved external implementation",
            evidence.implementation_id,
        )


def migrate_integration_evidence(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS integration_evidence (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, policy_digest TEXT NOT NULL,
        status TEXT NOT NULL, reason TEXT NOT NULL, evidence_key TEXT,
        payload TEXT NOT NULL, observed_at TEXT NOT NULL)""")


class IntegrationEvidenceStore:
    """Append-only persistence adapter; it never mutates jobs or GitHub state."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        migrate_integration_evidence(connection)

    def record(
        self,
        policy: IntegrationPolicy,
        decision: IntegrationDecision,
        prs: tuple[PullRequestEvidence, ...],
        external: ExternalImplementationEvidence | None,
    ) -> None:
        payload = {
            "policy": asdict(policy),
            "pull_requests": [asdict(pr) for pr in prs],
            "external": asdict(external) if external else None,
        }
        self.connection.execute(
            "INSERT INTO integration_evidence("
            "policy_digest,status,reason,evidence_key,payload,observed_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                json.dumps(asdict(policy), sort_keys=True),
                decision.status.value,
                decision.reason,
                decision.evidence_key,
                json.dumps(
                    payload,
                    sort_keys=True,
                    default=lambda v: v.value if isinstance(v, StrEnum) else v,
                ),
                decision.observed_at,
            ),
        )

    def list(self) -> builtins.list[dict[str, Any]]:
        cursor = self.connection.execute(
            "SELECT * FROM integration_evidence ORDER BY sequence"
        )
        columns = tuple(column[0] for column in cursor.description or ())
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class GitHubIntegrationSource:
    """Read-only GitHub evidence collector using the installed ``gh`` client.

    ``get`` may be injected in tests.  No endpoint here mutates GitHub state.
    """

    def __init__(
        self,
        get: Any | None = None,
        evaluator: IntegrationEvidenceEvaluator | None = None,
        graphql: Any | None = None,
    ) -> None:
        self._get_fn = get
        self.evaluator = evaluator
        self._graphql_fn = graphql

    def _get(self, endpoint: str) -> Any:
        if self._get_fn is not None:
            return self._get_fn(endpoint)
        completed = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(completed.stdout)

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self._graphql_fn is not None:
            value = self._graphql_fn(query, variables)
        else:
            completed = subprocess.run(
                ["gh", "api", "graphql", "--input", "-"],
                input=json.dumps({"query": query, "variables": variables}),
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            value = json.loads(completed.stdout)
        if not isinstance(value, dict) or value.get("errors"):
            raise ValueError("GitHub GraphQL evidence failed")
        return value

    def _closing_reference(
        self, repository: str, pr_number: int, issue_node_id: str
    ) -> bool:
        owner, name = repository.split("/", 1)
        cursor: str | None = None
        query = """query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            closingIssuesReferences(first:100, after:$cursor) {
              nodes { id number repository { nameWithOwner } }
              pageInfo { hasNextPage endCursor }
            }
          }}
        }"""
        for _ in range(10):
            payload = self._graphql(
                query,
                {"owner": owner, "name": name, "number": pr_number, "cursor": cursor},
            )
            refs = (
                ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
                or {}
            ).get("closingIssuesReferences")
            if not isinstance(refs, dict):
                raise ValueError("GitHub closing reference response is incomplete")
            for node in refs.get("nodes", ()):
                if (
                    node.get("id") == issue_node_id
                    and str(node.get("repository", {}).get("nameWithOwner", "")).lower()
                    == repository.lower()
                ):
                    return True
            page = refs.get("pageInfo", {})
            if not page.get("hasNextPage"):
                return False
            cursor = page.get("endCursor")
            if not cursor:
                raise ValueError("GitHub closing reference cursor is missing")
        raise ValueError("GitHub closing reference pagination is truncated")

    def target_commit(self, repository: str, branch: str) -> str:
        self._validate_repo(repository)
        if not branch or branch.startswith(("-", ".")) or ".." in branch:
            raise ValueError("invalid target branch")
        value = self._get(f"repos/{repository}/branches/{branch}")
        sha = value.get("commit", {}).get("sha")
        if not isinstance(sha, str) or not sha:
            raise ValueError("GitHub did not return an exact target commit")
        return sha

    def observe(
        self,
        issue: Any,
        base_commit: str,
        base_branch: str,
        *,
        linked_pr_numbers: tuple[int, ...] = (),
    ) -> IntegrationDecision:
        repository = getattr(issue, "repository", None) or issue["repository"]
        number = int(getattr(issue, "number", None) or issue["number"])
        issue_node_id = getattr(issue, "node_id", None)
        if issue_node_id is None and isinstance(issue, dict):
            issue_node_id = issue.get("node_id")
        if not issue_node_id:
            raise ValueError(
                "issue node identity is required for closing reference proof"
            )
        self._validate_repo(repository)
        pinned_base = exact_commit(base_commit)
        discovered: set[int] = set(linked_pr_numbers)
        mentions: set[int] = set()
        timeline_complete = False
        for page in range(1, 11):
            events = self._get(
                f"repos/{repository}/issues/{number}/timeline?per_page=100&page={page}"
            )
            for event in events:
                if event.get("event") in {"cross-referenced", "connected"}:
                    source = event.get("source", {}).get("issue", {})
                    source_repo = (
                        source.get("repository_url", "").rstrip("/").split("/")[-2:]
                    )
                    if (
                        source.get("pull_request")
                        and source.get("number")
                        and "/".join(source_repo).lower() == repository.lower()
                    ):
                        mentions.add(int(source["number"]))
            if len(events) < 100:
                timeline_complete = True
                break
        if not timeline_complete:
            raise ValueError("GitHub timeline pagination is truncated")
        current_target = self.target_commit(repository, base_branch)
        if current_target != pinned_base:
            raise ValueError("target branch moved after pinned base snapshot")
        for candidate in sorted(mentions):
            if self._closing_reference(repository, candidate, issue_node_id):
                discovered.add(candidate)
        result: list[PullRequestEvidence] = []
        for pr_number in sorted(discovered):
            data = self._get(f"repos/{repository}/pulls/{pr_number}")
            head = data.get("head", {}).get("sha")
            base = data.get("base", {})
            merged = bool(data.get("merged_at"))
            merged_sha = data.get("merge_commit_sha") if merged else None
            checks = self._checks(repository, head) if head else ()
            contains = (
                self._contains(repository, merged_sha, pinned_base)
                if merged_sha
                else None
            )
            reverted = (
                self._reverted(repository, merged_sha, pinned_base)
                if merged_sha
                else False
            )
            result.append(
                PullRequestEvidence(
                    repository=repository,
                    number=pr_number,
                    state=str(data.get("state", "closed")),
                    draft=bool(data.get("draft", False)),
                    head_sha=str(head or ""),
                    base_sha=str(base.get("sha", "")),
                    base_branch=str(base.get("ref", "")),
                    checks=checks,
                    merged=merged,
                    merged_sha=merged_sha,
                    target_contains_merge=contains,
                    reverted=reverted,
                    observed_at=datetime.now(UTC).isoformat(),
                )
            )
        if self.target_commit(repository, base_branch) != pinned_base:
            raise ValueError("target branch moved while collecting pull requests")
        # Ordinary cross-reference mentions have no completion authority and
        # are intentionally omitted from links/evidence. Explicit mappings
        # and exact native closing references identify implementation PRs.
        links = tuple(
            f"https://github.com/{repository}/pull/{n}" for n in sorted(discovered)
        )
        evaluator = self.evaluator or IntegrationEvidenceEvaluator(
            IntegrationPolicy(repository, base_branch)
        )
        decision = evaluator.evaluate(result)
        return IntegrationDecision(
            decision.status,
            decision.reason,
            decision.evidence_key,
            decision.observed_at,
            links,
            {
                "target_commit": pinned_base,
                "base_commit": pinned_base,
                "pull_requests": [asdict(pr) for pr in result],
            },
        )

    def _checks(self, repository: str, sha: str) -> tuple[CheckEvidence, ...]:
        checks: list[CheckEvidence] = []
        complete = False
        for page in range(1, 11):
            data = self._get(
                f"repos/{repository}/commits/{sha}/check-runs?per_page=100&page={page}"
            )
            checks.extend(
                CheckEvidence(
                    str(c.get("name", "")),
                    str(c.get("status", "")),
                    c.get("conclusion"),
                )
                for c in data.get("check_runs", ())
            )
            if len(data.get("check_runs", ())) < 100:
                complete = True
                break
        if not complete:
            raise ValueError("GitHub check-run pagination is truncated")
        status_data = self._get(f"repos/{repository}/commits/{sha}/status")
        checks.extend(
            CheckEvidence(
                str(status.get("context", "")), "completed", status.get("state")
            )
            for status in status_data.get("statuses", ())
        )
        return tuple(checks)

    def _contains(self, repository: str, merged_sha: str, target_commit: str) -> bool:
        data = self._get(f"repos/{repository}/compare/{merged_sha}...{target_commit}")
        # GitHub's compare status is ancestry evidence: identical means the
        # target is the merge commit; ahead means the merge commit is reachable.
        if data.get("total_commits", 0) >= 250 or data.get("truncated"):
            raise ValueError("GitHub compare result is truncated")
        return data.get("status") in {"identical", "ahead"}

    def _reverted(self, repository: str, merged_sha: str, target_commit: str) -> bool:
        data = self._get(f"repos/{repository}/compare/{merged_sha}...{target_commit}")
        if data.get("total_commits", 0) >= 250 or data.get("truncated"):
            raise ValueError("GitHub compare result is truncated")
        marker = merged_sha[:12].lower()
        return any(
            str(commit.get("commit", {}).get("message", ""))
            .lower()
            .startswith("revert")
            and marker in str(commit.get("commit", {}).get("message", "")).lower()
            for commit in data.get("commits", ())
        )

    @staticmethod
    def _validate_repo(repository: str) -> None:
        try:
            repository_name(repository)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "repository must be a canonical owner/name identity"
            ) from error
