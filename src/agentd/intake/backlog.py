"""Deterministic, read-only discovery of a native GitHub issue backlog.

The backlog reader deliberately has no policy or LLM behaviour.  It turns the
GitHub issue/dependency endpoints into an immutable graph snapshot that a
caller may persist and later use for authorization.  A partial read is useful
for inspection, but can never authorize work.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from agentd.coding.models import repository_name
from agentd.intake.models import IntakePolicy, SourceIssue


class ReadBound(StrEnum):
    """The provider read bound requested by a caller."""

    EXACT = "exact"
    BOUNDED = "bounded"


class BacklogTransport(Protocol):
    """Small transport surface, allowing authenticated ``gh`` adapters/fakes."""

    def repository(self, repository: str) -> dict[str, Any]: ...

    def issues(
        self, repository: str, *, page: int, per_page: int
    ) -> list[dict[str, Any]]: ...

    def blockers(self, repository: str, number: int) -> list[dict[str, Any]]: ...

    def issue(self, repository: str, number: int) -> dict[str, Any]: ...

    def sub_issues(
        self, repository: str, number: int, *, page: int, per_page: int
    ) -> list[dict[str, Any]]: ...


class GitHubAPITransport:
    """Authenticated read-only transport backed by the installed ``gh`` CLI."""

    def __init__(self, getter: Any | None = None) -> None:
        # Passing GitHubIssueSource._get reuses its authentication and timeout
        # policy; the standalone default keeps this adapter useful in tests and
        # small administrative tools.
        self._getter = getter

    def _get(self, endpoint: str) -> Any:
        if self._getter is not None:
            return self._getter(endpoint)
        completed = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(completed.stdout)

    def repository(self, repository: str) -> dict[str, Any]:
        return self._get(f"repos/{repository}")

    def issues(
        self, repository: str, *, page: int, per_page: int
    ) -> list[dict[str, Any]]:
        result = self._get(
            f"repos/{repository}/issues?state=all&per_page={per_page}&page={page}"
        )
        if not isinstance(result, list):
            raise ValueError("GitHub issues response is not a list")
        return result

    def issue(self, repository: str, number: int) -> dict[str, Any]:
        result = self._get(f"repos/{repository}/issues/{number}")
        if not isinstance(result, dict):
            raise ValueError("GitHub issue response is not an object")
        return result

    def sub_issues(
        self, repository: str, number: int, *, page: int, per_page: int
    ) -> list[dict[str, Any]]:
        result = self._get(
            f"repos/{repository}/issues/{number}/sub_issues?per_page={per_page}&page={page}"
        )
        if not isinstance(result, list):
            raise ValueError("GitHub sub-issues response is not a list")
        return result

    def blockers(self, repository: str, number: int) -> list[dict[str, Any]]:
        result = self._get(
            f"repos/{repository}/issues/{number}/dependencies/blocked_by"
        )
        if not isinstance(result, list):
            raise ValueError("GitHub dependency response is not a list")
        return result


@dataclass(frozen=True, slots=True)
class BacklogNode:
    """An issue and its stable dependency identities."""

    issue: SourceIssue
    blocked_by: tuple[str, ...] = ()
    issue_id: int | None = None

    @property
    def id(self) -> str:
        return self.issue.key

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.to_dict(),
            "blocked_by": list(self.blocked_by),
            "issue_id": self.issue_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BacklogNode:
        blocked_by = tuple(value.get("blocked_by", ()))
        if any(not isinstance(item, str) or not item for item in blocked_by):
            raise ValueError("blocked_by identities must be nonempty strings")
        raw_id = value.get("issue_id")
        return cls(
            SourceIssue.from_dict(value["issue"]),
            blocked_by,
            int(raw_id) if raw_id is not None else None,
        )


@dataclass(frozen=True, slots=True)
class BacklogSnapshot:
    """Immutable graph snapshot suitable for durable storage or replay."""

    repository: str
    repository_id: int
    nodes: tuple[BacklogNode, ...]
    order: tuple[str, ...]
    bound: ReadBound
    complete: bool
    pages_read: int
    page_limit: int
    members: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
    error: str | None = None
    revision: str = ""

    def __post_init__(self) -> None:
        if (
            self.repository != repository_name(self.repository)
            or self.repository_id <= 0
        ):
            raise ValueError(
                "snapshot requires a canonical repository and immutable ID"
            )
        ids = tuple(node.id for node in self.nodes)
        if len(ids) != len(set(ids)) or tuple(sorted(ids)) != ids:
            raise ValueError("snapshot nodes must be unique and sorted by stable ID")
        if set(self.order) != set(ids) or len(self.order) != len(ids):
            raise ValueError("snapshot order must contain every node exactly once")
        if not set(self.members).issubset(set(ids)):
            raise ValueError("snapshot members must refer to known nodes")
        if self.pages_read < 0 or self.page_limit <= 0:
            raise ValueError("invalid pagination metadata")
        calculated = _revision(self._revision_payload())
        if self.revision and self.revision != calculated:
            raise ValueError("snapshot revision does not match graph content")
        object.__setattr__(self, "revision", calculated)

    @property
    def issues(self) -> dict[str, SourceIssue]:
        return {node.id: node.issue for node in self.nodes}

    @property
    def blockers(self) -> dict[str, tuple[str, ...]]:
        return {node.id: node.blocked_by for node in self.nodes}

    @property
    def authorizable(self) -> bool:
        """Whether this snapshot is safe to use as an authorization source."""
        return (
            self.bound is ReadBound.EXACT
            and self.complete
            and not self.missing_dependencies
            and self.error is None
        )

    def selects(self, node_id: str, policy: IntakePolicy) -> bool:
        """Return true only when a complete, exact graph authorizes this node."""
        node = self.issues.get(node_id)
        return bool(
            self.authorizable
            and node_id in self.members
            and node is not None
            and policy.eligible(node)
        )

    def _revision_payload(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "repository_id": self.repository_id,
            "nodes": [
                {
                    "key": node.id,
                    "revision": node.issue.revision,
                    "blocked_by": list(node.blocked_by),
                }
                for node in self.nodes
            ],
            "members": list(self.members),
            "missing_dependencies": list(self.missing_dependencies),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "repository_id": self.repository_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "order": list(self.order),
            "bound": self.bound.value,
            "complete": self.complete,
            "pages_read": self.pages_read,
            "page_limit": self.page_limit,
            "members": list(self.members),
            "missing_dependencies": list(self.missing_dependencies),
            "error": self.error,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BacklogSnapshot:
        return cls(
            repository=str(value["repository"]),
            repository_id=int(value["repository_id"]),
            nodes=tuple(
                sorted(
                    (BacklogNode.from_dict(item) for item in value["nodes"]),
                    key=lambda n: n.id,
                )
            ),
            order=tuple(value["order"]),
            bound=ReadBound(value["bound"]),
            complete=bool(value["complete"]),
            pages_read=int(value["pages_read"]),
            page_limit=int(value["page_limit"]),
            members=tuple(sorted(value.get("members", value.get("order", ())))),
            missing_dependencies=tuple(sorted(value.get("missing_dependencies", ()))),
            error=value.get("error"),
            revision=str(value.get("revision", "")),
        )


class GitHubBacklogSource:
    """Read a repository's issues and native dependency edges deterministically."""

    def __init__(
        self,
        transport: BacklogTransport,
        *,
        per_page: int = 100,
        max_pages: int = 10,
        max_dependency_nodes: int = 1_000,
    ) -> None:
        if not 1 <= per_page <= 100 or max_pages <= 0 or max_dependency_nodes <= 0:
            raise ValueError("pagination bounds are invalid")
        self.transport = transport
        self.per_page = per_page
        self.max_pages = max_pages
        self.max_dependency_nodes = max_dependency_nodes

    def discover(
        self,
        repository: str,
        *,
        bound: ReadBound = ReadBound.BOUNDED,
        numbers: tuple[int, ...] = (),
        epic: int | None = None,
    ) -> BacklogSnapshot:
        repository = repository_name(repository)
        repo = self.transport.repository(repository)
        repository_id = int(repo["id"])
        selected = set(numbers)
        if epic is not None:
            if epic <= 0:
                raise ValueError("epic number must be positive")
            selected.add(epic)
        if any(number <= 0 for number in selected):
            raise ValueError("issue numbers must be positive")
        raw: list[dict[str, Any]] = []
        pages = 0
        complete = False
        error: str | None = None
        members: set[str] = set()
        if selected:
            # Explicit numbers and epics are exact point reads; a bounded scan
            # must never hide a requested issue beyond the page cap.
            for number in sorted(selected):
                try:
                    raw.append(self.transport.issue(repository, number))
                except Exception as exc:
                    error = error or f"issue {number}: {type(exc).__name__}: {exc}"
            complete = not error
            if epic is not None and not error:
                for page in range(1, self.max_pages + 1):
                    pages = max(pages, page)
                    try:
                        items = self.transport.sub_issues(
                            repository, epic, page=page, per_page=self.per_page
                        )
                    except Exception as exc:
                        error = (
                            error
                            or f"sub-issues page {page}: {type(exc).__name__}: {exc}"
                        )
                        break
                    raw.extend(items)
                    if len(items) < self.per_page:
                        break
                else:
                    complete = False
                    error = error or "exact read exceeded sub-issue pagination bound"
        else:
            for page in range(1, self.max_pages + 1):
                pages = page
                try:
                    items = self.transport.issues(
                        repository, page=page, per_page=self.per_page
                    )
                except (
                    Exception
                ) as exc:  # provider failures are represented, never inferred away
                    error = f"issue page {page}: {type(exc).__name__}: {exc}"
                    break
                raw.extend(items)
                if len(items) < self.per_page:
                    complete = True
                    break
        if bound is ReadBound.EXACT and not complete:
            error = error or "exact read exceeded pagination bound"
        decoded: dict[str, SourceIssue] = {}
        issue_ids: dict[str, int | None] = {}
        for item in raw:
            try:
                issue = _decode(repository, repository_id, item)
            except (KeyError, TypeError, ValueError) as exc:
                error = error or f"invalid issue payload: {exc}"
                continue
            decoded[issue.key] = issue
            issue_ids[issue.key] = (
                int(item["id"]) if item.get("id") is not None else None
            )
        nodes: list[BacklogNode] = []
        members = set(decoded)
        if epic is not None:
            members = {key for key, issue in decoded.items() if issue.number != epic}
        dependency_numbers: dict[str, int] = {}
        missing: set[str] = set()
        for issue in decoded.values():
            deps: list[str] = []
            try:
                edges = self.transport.blockers(repository, issue.number)
                for edge in edges:
                    dep = _dependency_id(repository, repository_id, edge)
                    deps.append(dep)
                    dependency_numbers[dep] = int(edge["number"])
            except Exception as exc:
                error = error or (
                    f"dependencies for issue {issue.number}: "
                    f"{type(exc).__name__}: {exc}"
                )
            nodes.append(
                BacklogNode(issue, tuple(sorted(set(deps))), issue_ids.get(issue.key))
            )
        # Native dependency responses identify issues by stable node ID. Fetch
        # dependency nodes recursively so graph topology is complete while
        # keeping them outside ``members`` (they are never auto-authorized).
        pending = [
            dep for node in nodes for dep in node.blocked_by if dep not in decoded
        ]
        seen = set(pending)
        while pending:
            if len(decoded) >= self.max_dependency_nodes:
                error = error or "dependency graph exceeded node bound"
                break
            dep_id = pending.pop(0)
            try:
                dep_number = dependency_numbers.get(dep_id)
                if dep_number is None:
                    raise ValueError("dependency number is unavailable")
                item = self.transport.issue(repository, dep_number)
                dep_issue = _decode(repository, repository_id, item)
                if dep_issue.key != dep_id:
                    raise ValueError("dependency identity changed")
                decoded[dep_id] = dep_issue
                issue_ids[dep_id] = (
                    int(item["id"]) if item.get("id") is not None else None
                )
                edges = self.transport.blockers(repository, dep_issue.number)
                dep_edges = tuple(
                    sorted(
                        {
                            _dependency_id(repository, repository_id, edge)
                            for edge in edges
                        }
                    )
                )
                dependency_numbers.update(
                    {
                        _dependency_id(repository, repository_id, edge): int(
                            edge["number"]
                        )
                        for edge in edges
                    }
                )
                nodes.append(BacklogNode(dep_issue, dep_edges, issue_ids.get(dep_id)))
                for child in dep_edges:
                    if child not in decoded and child not in seen:
                        seen.add(child)
                        pending.append(child)
            except Exception as exc:
                error = error or f"dependency {dep_id}: {type(exc).__name__}: {exc}"
        known = {node.id for node in nodes}
        for node in nodes:
            missing.update(dep for dep in node.blocked_by if dep not in known)
        order = _topological_order(nodes)
        if missing:
            error = error or "dependency graph references undiscovered issues"
        if _has_cycle(nodes):
            error = error or "dependency graph contains a cycle"
        return BacklogSnapshot(
            repository=repository,
            repository_id=repository_id,
            nodes=tuple(sorted(nodes, key=lambda node: node.id)),
            order=order,
            bound=bound,
            complete=complete and not (bound is ReadBound.EXACT and error),
            pages_read=pages,
            page_limit=self.max_pages,
            members=tuple(sorted(members)),
            missing_dependencies=tuple(sorted(missing)),
            error=error,
        )


def _decode(repository: str, repository_id: int, item: dict[str, Any]) -> SourceIssue:
    payload_repository = item.get("repository")
    if isinstance(payload_repository, dict):
        payload_repository = payload_repository.get("full_name")
    payload_url = item.get("repository_url")
    if (
        payload_url
        and str(payload_url).rstrip("/").split("/repos/")[-1].lower() != repository
    ):
        raise ValueError("issue payload belongs to another repository")
    if payload_repository and str(payload_repository).lower() != repository:
        raise ValueError("issue payload belongs to another repository")
    return SourceIssue(
        repository=repository,
        repository_id=repository_id,
        number=int(item["number"]),
        node_id=str(item["node_id"]),
        title=str(item["title"]),
        body=item.get("body") or "",
        updated_at=str(item["updated_at"]),
        state=str(item.get("state", "open")),
        labels=tuple(sorted(str(label["name"]) for label in item.get("labels", []))),
        is_pull_request="pull_request" in item,
    )


def _dependency_id(repository: str, repository_id: int, item: dict[str, Any]) -> str:
    """Decode a native dependency edge without trusting display names."""
    edge_repository = item.get("repository")
    if isinstance(edge_repository, dict):
        edge_repository = edge_repository.get("full_name")
    if edge_repository is not None and str(edge_repository).lower() != repository:
        raise ValueError("cross-repository dependency is outside the selected graph")
    if item.get("node_id"):
        # SourceIssue.key uses node_id; GitHub dependency payloads can omit title/body.
        return f"github:{repository_id}:{item['node_id']}"
    raise ValueError("dependency payload lacks immutable node_id")


def _topological_order(nodes: list[BacklogNode]) -> tuple[str, ...]:
    ids = {node.id for node in nodes}
    outgoing: dict[str, set[str]] = {node.id: set() for node in nodes}
    indegree = {node.id: 0 for node in nodes}
    for node in nodes:
        for dependency in node.blocked_by:
            if dependency in ids:
                outgoing[dependency].add(node.id)
                indegree[node.id] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    ready.sort()
    queue = deque(ready)
    result: list[str] = []
    while queue:
        current = queue.popleft()
        result.append(current)
        for successor in sorted(outgoing[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
        queue = deque(sorted(queue))
    # Cycles remain deterministic and visibly incomplete to callers.
    result.extend(sorted(ids.difference(result)))
    return tuple(result)


def _has_cycle(nodes: list[BacklogNode]) -> bool:
    """Check only discovered edges; unresolved edges are handled separately."""
    ids = {node.id for node in nodes}
    indegree = {node.id: sum(dep in ids for dep in node.blocked_by) for node in nodes}
    remaining = {node.id for node in nodes}
    while True:
        ready = sorted(item for item in remaining if indegree[item] == 0)
        if not ready:
            return bool(remaining)
        for current in ready:
            remaining.remove(current)
            for node in nodes:
                if current in node.blocked_by and node.id in remaining:
                    indegree[node.id] -= 1


def _revision(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BacklogNode",
    "BacklogSnapshot",
    "BacklogTransport",
    "GitHubAPITransport",
    "GitHubBacklogSource",
    "ReadBound",
]
