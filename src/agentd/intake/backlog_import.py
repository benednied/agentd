"""Reviewed, add-only native relationship imports with durable effect evidence."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentd.coding.models import fingerprint, repository_name
from agentd.intake.backlog import GitHubAPITransport, GitHubBacklogSource, ReadBound


@dataclass(frozen=True, slots=True)
class BacklogManifest:
    version: int
    repository: str
    repository_id: int
    epic: int
    members: tuple[int, ...]
    blocked_by: dict[int, tuple[int, ...]]

    def __post_init__(self) -> None:
        if self.version != 1 or self.repository != repository_name(self.repository):
            raise ValueError("manifest requires version 1 and canonical repository")
        numbers = {self.epic, *self.members}
        if self.repository_id <= 0 or any(
            type(n) is not int or n <= 0 for n in numbers
        ):
            raise ValueError("manifest requires positive stable identities")
        if self.epic in self.members or len(self.members) != len(set(self.members)):
            raise ValueError("members must be unique and exclude the epic")
        for source, deps in self.blocked_by.items():
            if source not in numbers or any(dep not in numbers for dep in deps):
                raise ValueError("manifest dependency is outside its explicit scope")
            if source in deps:
                raise ValueError("manifest contains a self dependency")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BacklogManifest:
        return cls(
            int(data["version"]),
            str(data["repository"]),
            int(data["repository_id"]),
            int(data["epic"]),
            tuple(data["members"]),
            {int(n): tuple(deps) for n, deps in data.get("blocked_by", {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "epic": self.epic,
            "members": sorted(self.members),
            "blocked_by": {
                str(n): sorted(set(deps)) for n, deps in sorted(self.blocked_by.items())
            },
        }


class NativeGraphImporter:
    def __init__(
        self,
        connection: sqlite3.Connection,
        source: GitHubBacklogSource | None = None,
        transport: Any = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        self.connection = connection
        self.transport = transport or GitHubAPITransport()
        self.source = source or GitHubBacklogSource(self.transport)
        self.request = request or self._request
        connection.execute("""CREATE TABLE IF NOT EXISTS backlog_import_audit (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, manifest_digest TEXT NOT NULL,
            actor TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        connection.commit()

    @staticmethod
    def _request(method: str, endpoint: str, payload: dict[str, int]) -> None:
        subprocess.run(
            ["gh", "api", "--method", method, endpoint, "--input", "-"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    def preview(self, manifest: BacklogManifest) -> dict[str, Any]:
        snapshot = self.source.discover(
            manifest.repository,
            bound=ReadBound.EXACT,
            numbers=manifest.members,
            epic=manifest.epic,
        )
        if (
            not snapshot.authorizable
            or snapshot.repository_id != manifest.repository_id
        ):
            raise ValueError(
                "native graph is incomplete or repository identity changed"
            )
        by_number = {node.issue.number: node for node in snapshot.nodes}
        if any(node.issue_id is None for node in snapshot.nodes):
            raise ValueError("native graph lacks numeric provider issue IDs")
        identities: dict[str, dict[str, Any]] = {}
        for number, node in by_number.items():
            assert node.issue_id is not None
            identities[str(number)] = {
                "id": node.issue_id,
                "key": node.id,
                "revision": node.issue.revision,
            }
        existing: set[tuple[str, int, int]] = set()
        for page in range(1, self.source.max_pages + 1):
            members = self.transport.sub_issues(
                manifest.repository, manifest.epic, page=page, per_page=100
            )
            for member in members:
                number = int(member["number"])
                if (
                    str(number) not in identities
                    or identities[str(number)]["id"] != member["id"]
                    or by_number[number].issue.node_id != member["node_id"]
                ):
                    raise ValueError("epic membership changed during preview")
                existing.add(("membership", manifest.epic, int(member["id"])))
            if len(members) < 100:
                break
        else:
            raise ValueError("native membership pagination is truncated")
        ids = {
            node.id: identities[str(node.issue.number)]["id"] for node in snapshot.nodes
        }
        for node in snapshot.nodes:
            existing.update(
                ("dependency", node.issue.number, ids[dep]) for dep in node.blocked_by
            )
        requested = {
            ("membership", manifest.epic, identities[str(n)]["id"])
            for n in manifest.members
        }
        requested.update(
            ("dependency", n, identities[str(dep)]["id"])
            for n, deps in manifest.blocked_by.items()
            for dep in deps
        )
        all_edges = {
            (identities[str(n)]["id"], dep)
            for kind, n, dep in existing | requested
            if kind == "dependency"
        }
        if _has_cycle(all_edges):
            raise ValueError("native graph plus manifest contains a cycle")
        inventory = {"identities": identities, "relationships": sorted(existing)}
        operations = sorted(requested - existing)
        revision = fingerprint(
            {
                "manifest": manifest.to_dict(),
                "inventory": inventory,
                "operations": operations,
            }
        )
        return {
            "revision": revision,
            "snapshot_revision": snapshot.revision,
            "repository": manifest.repository,
            "operations": operations,
            "inventory": inventory,
        }

    def _audit(self, revision: str, actor: str, action: str, payload: Any) -> None:
        self.connection.execute(
            "INSERT INTO backlog_import_audit(manifest_digest,actor,action,payload) "
            "VALUES (?,?,?,?)",
            (revision, actor, action, json.dumps(payload, sort_keys=True)),
        )
        self.connection.commit()

    def apply(
        self, manifest: BacklogManifest, expected_revision: str, actor: str
    ) -> tuple[tuple[str, int, int], ...]:
        if not actor.strip():
            raise ValueError("manifest import requires an administrative actor")
        initial = self.preview(manifest)
        if initial["revision"] != expected_revision:
            raise ValueError("manifest or native graph changed after preview")
        expected = initial["inventory"]
        applied = []
        for operation in initial["operations"]:
            fresh = self.preview(manifest)
            if fresh["inventory"] != expected:
                raise ValueError("native graph changed before mutation")
            kind, source, target_id = operation
            endpoint = f"repos/{manifest.repository}/issues/{source}/"
            if kind == "membership":
                endpoint += "sub_issues"
                payload = {"sub_issue_id": target_id}
            else:
                endpoint += "dependencies/blocked_by"
                payload = {"issue_id": target_id}
            self._audit(expected_revision, actor, "intended", operation)
            error = None
            try:
                self.request("POST", endpoint, payload)
            except Exception as caught:
                error = caught
            # Resolve lost responses from native relationships, without replaying POST.
            try:
                observed = self.preview(manifest)
            except Exception:
                self._audit(expected_revision, actor, "unresolved", operation)
                raise
            expected = {
                **expected,
                "relationships": sorted({*expected["relationships"], operation}),
            }
            if observed["inventory"] != expected:
                self._audit(expected_revision, actor, "unresolved", operation)
                raise ValueError(
                    "relationship effect is unresolved; preview before retry"
                ) from error
            self._audit(expected_revision, actor, "observed_applied", operation)
            applied.append(operation)
        return tuple(applied)


def _has_cycle(edges: set[tuple[int, int]]) -> bool:
    nodes = {n for edge in edges for n in edge}
    remaining = set(nodes)
    while remaining:
        ready = {
            n for n in remaining if not any(a == n and b in remaining for a, b in edges)
        }
        if not ready:
            return True
        remaining -= ready
    return False
