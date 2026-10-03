from __future__ import annotations

import sqlite3

import pytest

from agentd.intake.backlog import GitHubBacklogSource, ReadBound
from agentd.intake.backlog_import import (
    BacklogManifest,
    NativeGraphImporter,
)


def _issue(number: int, node: str) -> dict:
    return {
        "id": number * 100,
        "number": number,
        "node_id": node,
        "title": str(number),
        "body": "",
        "updated_at": "2026-09-20T12:00:00Z",
        "state": "open",
        "labels": [],
    }


class Transport:
    def __init__(self, edges=None, direct=None):
        self.edges = edges or {}
        self.direct = direct or {}

    def repository(self, repository):
        return {"id": 7}

    def issues(self, repository, *, page, per_page):
        return [_issue(1, "I1"), _issue(2, "I2")]

    def issue(self, repository, number):
        return self.direct.get(number, _issue(number, f"I{number}"))

    def blockers(self, repository, number):
        return self.edges.get(number, [])

    def sub_issues(self, repository, number, *, page, per_page):
        return []


def test_cross_repository_issue_and_dependency_payloads_fail_closed():
    transport = Transport(
        {1: [{"number": 2, "node_id": "I2", "repository": {"full_name": "evil/app"}}]}
    )
    snapshot = GitHubBacklogSource(transport).discover(
        "acme/app", bound=ReadBound.EXACT
    )
    assert not snapshot.authorizable
    assert "dependencies" in (snapshot.error or "")


def test_recursive_dependency_bound_fails_closed():
    edges = {n: [{"number": n + 1, "node_id": f"I{n + 1}"}] for n in range(1, 10)}
    transport = Transport(edges)
    snapshot = GitHubBacklogSource(transport, max_dependency_nodes=3).discover(
        "acme/app", bound=ReadBound.EXACT
    )
    assert not snapshot.authorizable
    assert "node bound" in (snapshot.error or "")


class MutableTransport(Transport):
    def __init__(self):
        super().__init__()
        self.members = set()
        self.calls = []
        self.lose_response = False

    def sub_issues(self, repository, number, *, page, per_page):
        return [_issue(n, f"I{n}") for n in sorted(self.members)] if page == 1 else []

    def request(self, method, endpoint, payload):
        self.calls.append((method, endpoint, payload))
        number = int(endpoint.split("/issues/")[1].split("/")[0])
        if endpoint.endswith("sub_issues"):
            self.members.add(payload["sub_issue_id"] // 100)
        else:
            dependency = payload["issue_id"] // 100
            self.edges.setdefault(number, []).append(
                _issue(dependency, f"I{dependency}")
            )
        if self.lose_response:
            raise TimeoutError("ack lost after effect")


def importer(tmp_path):
    transport = MutableTransport()
    db = sqlite3.connect(tmp_path / "audit.sqlite")
    return (
        NativeGraphImporter(
            db, GitHubBacklogSource(transport), transport, transport.request
        ),
        transport,
        db,
    )


def test_manifest_imports_membership_and_numeric_dependency_once(tmp_path):
    owner, transport, db = importer(tmp_path)
    manifest = BacklogManifest(1, "acme/app", 7, 1, (2,), {2: (1,)})
    preview = owner.preview(manifest)
    applied = owner.apply(manifest, preview["revision"], "operator")
    assert len(applied) == 2
    assert transport.calls[0] == (
        "POST",
        "repos/acme/app/issues/2/dependencies/blocked_by",
        {"issue_id": 100},
    )
    assert transport.calls[1] == (
        "POST",
        "repos/acme/app/issues/1/sub_issues",
        {"sub_issue_id": 200},
    )
    assert owner.apply(manifest, owner.preview(manifest)["revision"], "operator") == ()
    assert len(transport.calls) == 2
    assert db.execute("select action from backlog_import_audit").fetchall() == [
        ("intended",),
        ("observed_applied",),
        ("intended",),
        ("observed_applied",),
    ]


def test_import_rejects_manifest_drift_union_cycles_and_wrong_repository(tmp_path):
    owner, transport, _ = importer(tmp_path)
    manifest = BacklogManifest(1, "acme/app", 7, 1, (2,), {})
    preview = owner.preview(manifest)
    changed = BacklogManifest(1, "acme/app", 7, 1, (2,), {2: (1,)})
    with pytest.raises(ValueError, match="changed after preview"):
        owner.apply(changed, preview["revision"], "operator")
    transport.edges[1] = [_issue(2, "I2")]
    with pytest.raises(ValueError, match="cycle"):
        owner.preview(changed)
    with pytest.raises(ValueError, match="identity"):
        owner.preview(BacklogManifest(1, "acme/app", 99, 1, (2,), {}))
    assert not transport.calls


def test_import_audits_before_effect_and_resolves_lost_response(tmp_path):
    owner, transport, db = importer(tmp_path)
    manifest = BacklogManifest(1, "acme/app", 7, 1, (2,), {})
    native_request = transport.request

    def request(*args):
        with sqlite3.connect(tmp_path / "audit.sqlite") as observer:
            assert observer.execute(
                "select action from backlog_import_audit"
            ).fetchall() == [("intended",)]
        native_request(*args)

    owner.request = request
    transport.lose_response = True
    preview = owner.preview(manifest)
    assert len(owner.apply(manifest, preview["revision"], "operator")) == 1
    assert len(transport.calls) == 1
    assert db.execute("select action from backlog_import_audit").fetchall()[-1] == (
        "observed_applied",
    )
