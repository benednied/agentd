"""Durable, bounded administrative authority for native backlog snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.models import fingerprint
from agentd.domain.models import utc_now
from agentd.intake.backlog import BacklogSnapshot, GitHubBacklogSource, ReadBound
from agentd.intake.models import SourceIssue
from agentd.intake.service import GitHubIntake


def prerequisite_closure(snapshot: BacklogSnapshot, key: str) -> tuple[str, ...]:
    """Every reachable prerequisite, once, in deterministic graph order."""
    reached: set[str] = set()
    pending = list(snapshot.blockers[key])
    while pending:
        dependency = pending.pop()
        if dependency in reached:
            continue
        reached.add(dependency)
        pending.extend(snapshot.blockers[dependency])
    return tuple(node for node in snapshot.order if node in reached)


def node_revision(snapshot: BacklogSnapshot, key: str) -> str:
    """Approval covers intent and all prerequisite identities and intent."""
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        visited.add(current)
        for dependency in snapshot.blockers[current]:
            visit(dependency)

    visit(key)
    return fingerprint(
        {
            current: {
                "revision": snapshot.issues[current].revision,
                "blockers": snapshot.blockers[current],
            }
            for current in sorted(visited)
        }
    )


class BacklogLedger:
    """Append-only snapshots/decisions; one replaceable administrative grant."""

    def __init__(self, database: str | Path, selection: dict[str, Any]) -> None:
        self.path = database
        self.selection_id = fingerprint(selection)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS backlog_snapshots (
                    selection_id TEXT NOT NULL, revision TEXT NOT NULL,
                    payload TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(selection_id, revision));
                CREATE TABLE IF NOT EXISTS backlog_grants (
                    selection_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backlog_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    selection_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, occurred_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backlog_status (
                    selection_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backlog_integration_evidence (
                    selection_id TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                    payload TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(selection_id, evidence_digest));
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def observe(self, snapshot: BacklogSnapshot) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO backlog_snapshots VALUES (?, ?, ?, ?)",
                (
                    self.selection_id,
                    snapshot.revision,
                    json.dumps(snapshot.to_dict(), sort_keys=True),
                    utc_now().isoformat(),
                ),
            )

    def approve(
        self, snapshot: BacklogSnapshot, *, actor: str, mode: str = "exact"
    ) -> dict[str, Any]:
        if not actor.strip() or mode not in {"exact", "bounded"}:
            raise ValueError("graph approval requires an actor and exact/bounded mode")
        if not snapshot.authorizable:
            raise ValueError("incomplete graph cannot be approved")
        grant = {
            "actor": actor,
            "mode": mode,
            "revision": snapshot.revision,
            "nodes": {key: node_revision(snapshot, key) for key in snapshot.members},
            "approved_at": utc_now().isoformat(),
        }
        self.observe(snapshot)
        with self.connect() as db:
            db.execute(
                "INSERT INTO backlog_grants VALUES (?, ?) "
                "ON CONFLICT(selection_id) DO UPDATE SET payload=excluded.payload",
                (self.selection_id, json.dumps(grant, sort_keys=True)),
            )
            db.execute(
                "INSERT INTO backlog_events(selection_id,kind,payload,occurred_at) "
                "VALUES (?, 'approved', ?, ?)",
                (
                    self.selection_id,
                    json.dumps(grant, sort_keys=True),
                    grant["approved_at"],
                ),
            )
        return grant

    def grant(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM backlog_grants WHERE selection_id=?",
                (self.selection_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def authorized(self, snapshot: BacklogSnapshot, key: str) -> bool:
        grant = self.grant()
        return bool(
            snapshot.authorizable
            and key in snapshot.members
            and grant
            and (grant["mode"] == "bounded" or grant["revision"] == snapshot.revision)
            and grant["nodes"].get(key) == node_revision(snapshot, key)
        )

    def report(self, payload: dict[str, Any]) -> bool:
        """Persist transitions and suppress unchanged notifications after restart."""
        encoded = json.dumps(payload, sort_keys=True)
        with self.connect() as db:
            old = db.execute(
                "SELECT payload FROM backlog_status WHERE selection_id=?",
                (self.selection_id,),
            ).fetchone()
            changed = old is None or old[0] != encoded
            now = utc_now().isoformat()
            db.execute(
                "INSERT INTO backlog_status VALUES (?, ?, ?) "
                "ON CONFLICT(selection_id) DO UPDATE SET "
                "payload=excluded.payload, observed_at=excluded.observed_at",
                (self.selection_id, encoded, now),
            )
            if changed:
                db.execute(
                    "INSERT INTO backlog_events(selection_id,kind,payload,occurred_at) "
                    "VALUES (?, 'status', ?, ?)",
                    (self.selection_id, encoded, now),
                )
        return changed

    def status(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload, observed_at FROM backlog_status WHERE selection_id=?",
                (self.selection_id,),
            ).fetchone()
        return {**json.loads(row[0]), "observed_at": row[1]} if row else None

    def evidence(self, issue: SourceIssue, decision: Any, base: str) -> None:
        def stable(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k != "observed_at"}
            if isinstance(value, (tuple, list)):
                return [stable(v) for v in value]
            return value

        payload = stable(
            {
                "issue": issue.key,
                "base_commit": base,
                "ready": decision.ready,
                "reason": decision.reason,
                "links": list(decision.links),
                "evidence": dict(decision.evidence),
            }
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO backlog_integration_evidence VALUES (?, ?, ?, ?) "
                "ON CONFLICT(selection_id,evidence_digest) DO UPDATE SET "
                "observed_at=excluded.observed_at",
                (
                    self.selection_id,
                    fingerprint(payload),
                    json.dumps(payload, sort_keys=True),
                    utc_now().isoformat(),
                ),
            )


class BacklogReconciler:
    """The source tick gates all admission before any job can become READY."""

    def __init__(
        self,
        intake: GitHubIntake,
        source: GitHubBacklogSource,
        integration: Any,
        config: dict[str, Any],
    ) -> None:
        self.intake, self.source, self.integration = intake, source, integration
        self.config = config
        self.repository = config["profile"]["repository"]
        self.selection = config["backlog"]
        if bool(self.selection.get("issues")) == bool(self.selection.get("epic")):
            raise ValueError("backlog requires either a nonempty issue set or one epic")
        self.ledger = BacklogLedger(
            intake.store.path, {"repository": self.repository, **self.selection}
        )
        compiler = intake.compile_job
        if not isinstance(compiler, CodingJobCompiler):
            raise ValueError("backlog requires a typed coding compiler")
        self.compiler = compiler

    def linked_pr_numbers(self, issue: SourceIssue) -> tuple[int, ...]:
        from urllib.parse import urlparse

        from agentd.publication import PublicationStore

        numbers = set(
            self.selection.get("pull_requests", {}).get(str(issue.number), ())
        )
        # Our publication ledger is trusted provenance; issue prose and arbitrary
        # cross-references are not proof that a PR implements this issue.
        publication = PublicationStore(self.intake.store.path).get(issue.job_id)
        if publication and publication["pr"]:
            url = urlparse(publication["pr"].get("url", ""))
            prefix = f"/{issue.repository}/pull/"
            if url.hostname == "github.com" and url.path.startswith(prefix):
                number = url.path[len(prefix) :]
                if number.isdecimal():
                    numbers.add(int(number))
        if any(type(n) is not int or n <= 0 for n in numbers):
            raise ValueError(
                "pull request mapping requires positive integer identities"
            )
        return tuple(sorted(numbers))

    def discover(self) -> BacklogSnapshot:
        snapshot = self.source.discover(
            self.repository,
            bound=ReadBound.EXACT,
            numbers=tuple(self.selection.get("issues", ())),
            epic=self.selection.get("epic"),
        )
        if snapshot.repository_id != self.config["repository_id"]:
            raise ValueError("backlog repository identity changed")
        self.ledger.observe(snapshot)
        return snapshot

    def approve(self, *, actor: str, revision: str, mode: str) -> dict[str, Any]:
        snapshot = self.discover()
        if revision != snapshot.revision:
            raise ValueError("graph changed since preview; inspect and approve again")
        return self.ledger.approve(snapshot, actor=actor, mode=mode)

    def _gate(
        self,
        issue: SourceIssue,
        revision: str,
        ready: bool,
        base: str | None,
        reason: str,
        *,
        defer_queued: bool = True,
    ) -> None:
        now = utc_now()
        self.intake.store.set_backlog_gate(
            issue.job_id,
            graph_revision=revision,
            ready=ready,
            base_commit=base,
            reason=reason,
            checked_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=120)).isoformat(),
            defer_queued=defer_queued,
        )

    def fence(self, reason: str, *, defer_queued: bool = True) -> None:
        """Block old queued jobs before network calls, including removed members."""
        for job in self.intake.store.list_jobs():
            record = self.intake.store.github_source_for_job(job.id)
            if record is None:
                continue
            issue = SourceIssue.from_dict(json.loads(record["payload"]))
            if issue.repository == self.repository:
                self._gate(
                    issue, "unrefreshed", False, None, reason, defer_queued=defer_queued
                )

    def plan(self, snapshot: BacklogSnapshot) -> dict[str, Any]:
        result: dict[str, Any] = {
            "repository": snapshot.repository,
            "revision": snapshot.revision,
            "complete": snapshot.authorizable,
            "error": snapshot.error,
            "nodes": [],
        }
        if not snapshot.authorizable:
            return result
        base = self.integration.target_commit(
            self.repository, self.config["base_branch"]
        )
        result["base_commit"] = base
        evidence: dict[str, Any] = {}
        for key in snapshot.order:
            issue = snapshot.issues[key]
            evidence[key] = self.integration.observe(
                issue,
                base_commit=base,
                base_branch=self.config["base_branch"],
                linked_pr_numbers=self.linked_pr_numbers(issue),
            )
            self.ledger.evidence(issue, evidence[key], base)
        policy = self.intake.policies[self.repository]
        for key in snapshot.order:
            if key not in snapshot.members:
                continue
            issue = snapshot.issues[key]
            own = evidence[key]
            binding = self.intake.store.backlog_binding(issue.job_id)
            graph_changed = bool(
                self.intake.store.list_runs(issue.job_id)
                and (
                    binding is None
                    or binding["node_revision"] != node_revision(snapshot, key)
                )
            )
            blockers = [
                {
                    "issue": snapshot.issues[dep].number,
                    "reason": evidence[dep].reason,
                    "links": list(evidence[dep].links),
                }
                for dep in prerequisite_closure(snapshot, key)
                if not evidence[dep].ready
            ]
            ready = False
            if not self.ledger.authorized(snapshot, key):
                reason = "graph_approval_required"
            elif not policy.eligible(issue):
                reason = "source_ineligible"
            elif own.ready:
                reason = "already_integrated"
            elif own.links:
                reason = "existing_pull_request: " + own.reason
            elif graph_changed:
                reason = "executed_job_graph_changed"
            elif blockers:
                reason = "waiting_for_prerequisite_integration"
            elif str(issue.number) in self.selection.get("product_decisions", {}):
                reason = "product_decision_required"
            else:
                ready, reason = True, "dependency_ready"
            result["nodes"].append(
                {
                    "key": key,
                    "issue": issue.number,
                    "job_id": issue.job_id,
                    "ready": ready,
                    "reason": reason,
                    "blockers": blockers,
                    "links": list(own.links),
                    "issue_url": f"https://github.com/{issue.repository}/issues/{issue.number}",
                }
            )
        return result

    async def poll(self) -> tuple[str, ...]:
        import asyncio

        self.fence("backlog_refresh_pending", defer_queued=False)
        try:
            snapshot = await asyncio.to_thread(self.discover)
            plan = await asyncio.to_thread(self.plan, snapshot)
            if not snapshot.authorizable:
                raise ValueError(snapshot.error or "incomplete backlog graph")
            for item in plan["nodes"]:
                issue = snapshot.issues[item["key"]]
                try:
                    existing = self.intake.store.get_job(issue.job_id)
                except LookupError:
                    existing = None
                base = plan["base_commit"]
                if existing and self.intake.store.list_runs(existing.id):
                    base = existing.base_ref
                self._gate(
                    issue, snapshot.revision, item["ready"], base, item["reason"]
                )
                policy = self.intake.policies[self.repository]
                decision = self.intake.store.observe_github_issue(issue, policy)
                if not item["ready"]:
                    continue
                if decision != "approved":
                    grant = self.ledger.grant()
                    if grant is None:
                        raise ValueError(
                            "graph approval disappeared during reconciliation"
                        )
                    self.intake.store.approve_github_issue(
                        issue, policy, actor="backlog:" + grant["actor"]
                    )
                compiler = replace(self.compiler, base_commit=base)
                self.intake.store.bind_backlog_job(
                    issue.job_id, node_revision(snapshot, issue.key), base
                )
                self.intake.store.create_github_job(issue, compiler(issue))
            for job in self.intake.store.list_jobs():
                gate = self.intake.store.backlog_gate(job.id)
                source = self.intake.store.github_source_for_job(job.id)
                if gate and gate["graph_revision"] == "unrefreshed" and source:
                    self._gate(
                        SourceIssue.from_dict(json.loads(source["payload"])),
                        snapshot.revision,
                        False,
                        None,
                        "graph_membership_removed",
                    )
            if self.ledger.report(plan):
                print(json.dumps({"backlog": plan}, sort_keys=True), flush=True)
            return tuple(item["key"] for item in plan["nodes"])
        except Exception as error:
            self.fence("backlog_refresh_failed")
            failure = {
                "repository": self.repository,
                "reason": "source_unavailable",
                "error_type": type(error).__name__,
            }
            if self.ledger.report(failure):
                print(json.dumps({"backlog": failure}, sort_keys=True), flush=True)
            raise

    def refresh_authorization(self, issue: SourceIssue) -> None:
        """Publication/resume also need current graph authority and prerequisites."""
        snapshot = self.discover()
        if not self.ledger.authorized(snapshot, issue.key):
            raise ValueError("backlog approval changed")
        if snapshot.issues[issue.key].revision != issue.revision:
            raise ValueError("backlog issue changed")
        binding = self.intake.store.backlog_binding(issue.job_id)
        if binding is None or binding["node_revision"] != node_revision(
            snapshot, issue.key
        ):
            raise ValueError("executed backlog authorization cannot change")
        self.intake.refresh_authorization(issue)
        base = self.integration.target_commit(
            self.repository, self.config["base_branch"]
        )
        for dep in prerequisite_closure(snapshot, issue.key):
            prerequisite = snapshot.issues[dep]
            evidence = self.integration.observe(
                prerequisite,
                base_commit=base,
                base_branch=self.config["base_branch"],
                linked_pr_numbers=self.linked_pr_numbers(prerequisite),
            )
            if not evidence.ready:
                raise ValueError(
                    "prerequisite no longer integrated: " + evidence.reason
                )
