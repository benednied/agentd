"""Trusted host retirement of one explicitly selected, unmetered coding attempt.

GitHub supplies a bound human command, never a container, file path or shell
command. Controller and worker ledgers are read only here. The administrative
controller RPC owns quarantine; this tool only fences physical ownership.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPOSITORY_ID = 1328873039
REPOSITORY = "benednied/agentd"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_UNRESOLVED = {"STARTING", "RUNNING", "DRAINING", "CHECKPOINTED"}
_SERVICES = ("coding-controller", "coding-publisher", "coding-worker")
_DONE = {"complete", "rejected", "declined", "retry_authorized"}


class AbandonBlocked(RuntimeError):
    """A compact, credential-free physical retirement gate failed."""


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected a UTC timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("Expected a UTC timestamp")
    return result


def fresh_abandon(
    comment: dict[str, Any],
    *,
    actors: frozenset[str],
    actor_ids: dict[str, int],
    activated_at: str,
) -> tuple[str, str | None] | None:
    """Accept only a new unedited, exact command from an immutable trusted user."""
    actor = comment.get("user") or {}
    identity, login = actor.get("id"), actor.get("login")
    comment_id = comment.get("id")
    body = comment.get("body")
    if (
        login not in actors
        or not isinstance(identity, int)
        or isinstance(identity, bool)
        or actor_ids.get(login) != identity
        or not isinstance(comment_id, int)
        or isinstance(comment_id, bool)
        or comment_id <= 0
        or not isinstance(comment.get("node_id"), str)
        or not comment["node_id"]
        or not isinstance(body, str)
        or "<!--" in body
    ):
        return None
    matched = re.fullmatch(
        r"/agentd abandon(?: ([A-Za-z0-9][A-Za-z0-9._-]{0,127}))?", body.strip()
    )
    if matched is None:
        return None
    try:
        if comment["created_at"] != comment["updated_at"] or instant(
            comment["created_at"]
        ) <= instant(activated_at):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return login, matched.group(1)


def readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise AbandonBlocked("recovery_ledger_missing")
    connection = sqlite3.connect(
        path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def metered(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("usage") is not None
        and (result.get("metadata") or {}).get("telemetry_valid") is True
    )


class HostAbandon:
    def __init__(self, watcher: Any, config: dict[str, Any]) -> None:
        self.watcher, self.config = watcher, config
        self.controller = Path(config["controller_database"]).absolute()
        self.journal = Path(config["worker_journal"]).absolute()
        self.proofs = Path(config["worker_proof_directory"]).absolute()
        self.node, self.epoch = config["node_id"], config["session_epoch"]
        self.current = watcher.root.parent / "coding-current"
        self.path = watcher.status_file.parent / "github-abandon.sqlite"
        self.maximum_attempts = config.get("maximum_attempts", 3)
        instant(config["activated_at"])
        if (
            not watcher.actor_ids
            or any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in (self.node, self.epoch)
            )
            or self.proofs != self.journal.parent / "quarantine-stops"
            or not isinstance(self.maximum_attempts, int)
            or isinstance(self.maximum_attempts, bool)
            or not 1 <= self.maximum_attempts <= 5
        ):
            raise ValueError(
                "Abandon requires immutable actors and protected fixed worker identity"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS host_abandon_events ("
                "event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "phase TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, error TEXT, "
                "report_id INTEGER, reported_body TEXT)"
            )
        self.path.chmod(0o600)

    def save(self, event: dict[str, Any]) -> None:
        self.save_many([event])

    def save_many(self, events: list[dict[str, Any]]) -> None:
        with sqlite3.connect(self.path) as database:
            database.executemany(
                "INSERT INTO host_abandon_events VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload, "
                "phase=excluded.phase, attempts=excluded.attempts, "
                "error=excluded.error, "
                "report_id=excluded.report_id, reported_body=excluded.reported_body",
                [
                    (
                        event["event_id"],
                        json.dumps(event["payload"], sort_keys=True),
                        event["phase"],
                        event.get("attempts", 0),
                        event.get("error"),
                        event.get("report_id"),
                        event.get("reported_body"),
                    )
                    for event in events
                ],
            )

    def events(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as database:
            database.row_factory = sqlite3.Row
            return [
                {**dict(row), "payload": json.loads(row["payload"])}
                for row in database.execute(
                    "SELECT * FROM host_abandon_events ORDER BY rowid"
                )
            ]

    def phase(self, event: dict[str, Any], phase: str) -> None:
        event.update(phase=phase, error=None)
        self.save(event)

    def resolve_run(
        self, subject_number: int, run_id: str | None = None
    ) -> dict[str, Any]:
        """Prove source/PR mapping and a unique unknown START from read-only ledgers."""
        with closing(readonly(self.controller)) as database:
            database.execute("BEGIN")
            sources = {}
            for row in database.execute(
                "SELECT source_key, job_id, payload FROM github_sources "
                "WHERE job_id IS NOT NULL"
            ):
                source = json.loads(row["payload"])
                if (
                    source.get("repository") == REPOSITORY
                    and source.get("repository_id") == REPOSITORY_ID
                    and row["source_key"]
                    == f"github:{REPOSITORY_ID}:{source.get('node_id')}"
                ):
                    sources[row["job_id"]] = source
            jobs = {
                job
                for job, source in sources.items()
                if source.get("number") == subject_number
            }
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            publications = list(
                database.execute(
                    "SELECT job_id, intent, pr FROM draft_publications "
                    "WHERE pr IS NOT NULL"
                )
            )
            if "publication_candidates" in tables:
                publications.extend(
                    database.execute(
                        "SELECT job_id, intent, pr FROM publication_candidates "
                        "WHERE stage='published' AND pr IS NOT NULL"
                    )
                )
            for row in publications:
                intent, delivery = json.loads(row["intent"]), json.loads(row["pr"])
                if (
                    intent.get("repository") == REPOSITORY
                    and delivery.get("url")
                    == f"https://github.com/{REPOSITORY}/pull/{subject_number}"
                    and row["job_id"] in sources
                ):
                    jobs.add(row["job_id"])
            if subject_number == self.watcher.config.get("status_issue_number"):
                if run_id is None:
                    raise AbandonBlocked("operations_abandon_requires_run_id")
                row = database.execute(
                    "SELECT job_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                jobs = {row["job_id"]} if row and row["job_id"] in sources else set()
            if len(jobs) != 1:
                raise AbandonBlocked("abandon_subject_has_no_unique_source_job")
            job_id = jobs.pop()
            rows = database.execute(
                "SELECT id, state, payload FROM runs WHERE job_id=? "
                "ORDER BY started_at DESC, id DESC",
                (job_id,),
            ).fetchall()
            if not rows or (run_id is not None and rows[0]["id"] != run_id):
                raise AbandonBlocked("abandon_requires_latest_source_run")
            selected = rows[0]
            run = json.loads(selected["payload"])
            if (
                selected["state"] in {"QUARANTINED", "SUSPENDED"}
                or run.get("node_id") != self.node
                or run.get("job_id") != job_id
                or run.get("id") != selected["id"]
                or _IDENTIFIER.fullmatch(run.get("id", "")) is None
                or (run.get("contract") or {}).get("job_id") != job_id
                or metered(run.get("result"))
            ):
                raise AbandonBlocked("abandon_run_is_not_unknown_worker_attempt")
            reservation = database.execute(
                "SELECT state FROM quota_reservations WHERE id=? AND job_id=?",
                (run.get("reservation_id"), job_id),
            ).fetchone()
            allocation = database.execute(
                "SELECT state FROM resource_allocations "
                "WHERE id=? AND job_id=? AND node_id=?",
                (run.get("allocation_id"), job_id, self.node),
            ).fetchone()
            if (
                not reservation
                or reservation["state"] not in {"ACTIVE", "METERING_PENDING"}
                or not allocation
                or allocation["state"] != "ACTIVE"
            ):
                raise AbandonBlocked("abandon_requires_retained_unknown_allocation")
            for other in database.execute(
                "SELECT id, state, payload FROM runs WHERE id!=?", (run["id"],)
            ):
                if (
                    other["state"] in _UNRESOLVED
                    and json.loads(other["payload"]).get("node_id") == self.node
                ):
                    raise AbandonBlocked("worker_has_other_active_run")
            start_hash = hashlib.sha256(
                canonical(
                    {
                        "driver": run["driver"],
                        "contract": run["contract"],
                        "managed": True,
                    }
                )
            ).hexdigest()
            source = sources[job_id]
        with closing(readonly(self.journal)) as database:
            database.execute("BEGIN")
            claims = database.execute(
                "SELECT * FROM worker_run_claims WHERE node_id=? AND session_epoch=?",
                (self.node, self.epoch),
            ).fetchall()
            target = [row for row in claims if row["run_id"] == run["id"]]
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            retired = (
                {
                    row[0]
                    for row in database.execute(
                        "SELECT run_id FROM worker_run_retirements"
                    )
                }
                if "worker_run_retirements" in tables
                else set()
            )
            results = {
                row["run_id"]: json.loads(row["result"])
                for row in database.execute(
                    "SELECT run_id,result FROM worker_run_results "
                    "WHERE node_id=? AND session_epoch=?",
                    (self.node, self.epoch),
                )
            }
            if (
                len(target) != 1
                or target[0]["start_hash"] != start_hash
                or run["id"] in retired
                or metered(results.get(run["id"]))
            ):
                raise AbandonBlocked("immutable_worker_start_is_not_unknown")
            if any(
                row["run_id"] != run["id"]
                and row["run_id"] not in results
                and row["run_id"] not in retired
                for row in claims
            ):
                raise AbandonBlocked("worker_has_other_unretired_start")
        return {
            "run_id": run["id"],
            "job_id": job_id,
            "node_id": self.node,
            "session_epoch": self.epoch,
            "start_hash": start_hash,
            "source_issue": source["number"],
            "source_node_id": source["node_id"],
        }

    def compose(self, *args: str, **kwargs: Any) -> str:
        return self.watcher.command(
            (str(self.current / "deploy/scripts/coding-compose.sh"), *args), **kwargs
        )

    def inspect(self, identifier: str) -> dict[str, Any]:
        if _CONTAINER.fullmatch(identifier) is None:
            raise AbandonBlocked("recovery_container_identity_invalid")
        response = json.loads(
            self.watcher.command(("/usr/bin/docker", "inspect", identifier), timeout=30)
        )
        if (
            not isinstance(response, list)
            or len(response) != 1
            or not isinstance(response[0], dict)
            or response[0].get("Id") != identifier
        ):
            raise AbandonBlocked("recovery_container_identity_changed")
        return response[0]

    def containers(self) -> dict[str, str]:
        found = {}
        for service in _SERVICES:
            identifiers = self.compose(
                "ps", "--all", "--quiet", service, timeout=30
            ).splitlines()
            if len(identifiers) != 1:
                raise AbandonBlocked("recovery_service_container_ambiguous")
            info = self.inspect(identifiers[0])
            labels = (info.get("Config") or {}).get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != "agentd-selfhost-coding"
                or labels.get("com.docker.compose.service") != service
            ):
                raise AbandonBlocked("recovery_container_service_mismatch")
            if service == "coding-worker":
                arguments = (info.get("Config") or {}).get("Cmd") or []
                if not isinstance(arguments, list):
                    raise AbandonBlocked("recovery_worker_epoch_mismatch")
                for flag, expected in (
                    ("--node-id", self.node),
                    ("--session-epoch", self.epoch),
                ):
                    if flag not in arguments or arguments[
                        arguments.index(flag) + 1 : arguments.index(flag) + 2
                    ] != [expected]:
                        raise AbandonBlocked("recovery_worker_epoch_mismatch")
            found[service] = identifiers[0]
        return found

    def units(self, action: str, *services: str) -> None:
        self.watcher.command(
            (
                "/usr/bin/systemctl",
                "--user",
                action,
                *(
                    "agentd-selfhost-" + service.removeprefix("coding-") + ".service"
                    for service in services
                ),
            ),
            timeout=150,
        )

    def require_stopped(self, container: str) -> None:
        state = self.inspect(container).get("State") or {}
        if (
            state.get("Running") is not False
            or state.get("Pid") != 0
            or isinstance(state.get("Pid"), bool)
        ):
            raise AbandonBlocked("exact_worker_container_not_stopped")

    def current_intent(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        if (
            str(self.current.resolve()) != payload["release"]
            or self.containers() != payload["containers"]
        ):
            raise AbandonBlocked("recovery_release_or_container_changed")

    def refresh_command(self, event: dict[str, Any]) -> None:
        payload = event["payload"].get("authorization", event["payload"])
        comment = self.watcher.github(
            f"repos/{REPOSITORY}/issues/comments/{payload['comment_id']}"
        )
        if (
            fresh_abandon(
                comment,
                actors=self.watcher.actors,
                actor_ids=self.watcher.actor_ids,
                activated_at=self.config["activated_at"],
            )
            != (payload["actor"], payload["requested_run_id"])
            or comment.get("node_id") != payload["comment_node_id"]
            or comment.get("issue_url")
            != f"https://api.github.com/repos/{REPOSITORY}/issues/{payload['subject_number']}"
        ):
            raise AbandonBlocked("abandon_command_changed")

    def write_proof(self, event: dict[str, Any]) -> Path:
        payload = event["payload"]
        proof = {
            key: payload[key]
            for key in (
                "run_id",
                "job_id",
                "node_id",
                "session_epoch",
                "start_hash",
                "actor",
            )
        }
        proof.update(
            proof_version=1,
            event_id=event["event_id"],
            container_id=payload["containers"]["coding-worker"],
            stopped_at=payload["stopped_at"],
            running=False,
            pid=0,
        )
        self.proofs.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            self.proofs.is_symlink()
            or self.proofs.stat().st_uid != os.getuid()
            or self.proofs.stat().st_mode & 0o777 != 0o700
        ):
            raise AbandonBlocked("protected_stop_directory_permissions_invalid")
        name = (
            hashlib.sha256(
                canonical({"run_id": payload["run_id"], "event_id": event["event_id"]})
            ).hexdigest()
            + ".json"
        )
        path = self.proofs / name
        if path.exists() or path.is_symlink():
            if (
                path.is_symlink()
                or path.stat().st_uid != os.getuid()
                or path.stat().st_mode & 0o777 != 0o600
                or json.loads(path.read_text()) != proof
            ):
                raise AbandonBlocked("protected_stop_proof_conflict")
            return path
        descriptor, temporary = tempfile.mkstemp(prefix=".stop-proof-", dir=self.proofs)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical(proof))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)  # No overwrite after a competing proof creation.
            directory = os.open(self.proofs, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def quarantined(self, event: dict[str, Any]) -> bool:
        with closing(readonly(self.controller)) as database:
            row = database.execute(
                "SELECT run_id,job_id,event_id,actor "
                "FROM run_quarantines WHERE run_id=?",
                (event["payload"]["run_id"],),
            ).fetchone()
            run = database.execute(
                "SELECT state FROM runs WHERE id=?", (event["payload"]["run_id"],)
            ).fetchone()
            return bool(
                row
                and run
                and run["state"] == "QUARANTINED"
                and row["job_id"] == event["payload"]["job_id"]
                and row["event_id"] == event["event_id"]
                and row["actor"] == event["payload"]["actor"]
            )

    def execute(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        self.current_intent(event)
        if event["phase"] in {"accepted", "controllers_stopping"}:
            self.refresh_command(event)
            self.phase(event, "controllers_stopping")
            self.units("stop", "coding-controller", "coding-publisher")
            for service in _SERVICES[:2]:
                self.require_stopped(payload["containers"][service])
            self.phase(event, "controllers_stopped")
        if event["phase"] in {"controllers_stopped", "worker_stopping"}:
            self.refresh_command(event)
            bound = self.resolve_run(payload["subject_number"], payload["run_id"])
            if any(payload[key] != value for key, value in bound.items()):
                raise AbandonBlocked("abandon_target_changed")
            for service in _SERVICES[:2]:
                self.require_stopped(payload["containers"][service])
            self.phase(event, "worker_stopping")
            self.units("stop", "coding-worker")
            self.watcher.command(
                (
                    "/usr/bin/docker",
                    "stop",
                    "--time",
                    "20",
                    payload["containers"]["coding-worker"],
                ),
                timeout=45,
            )
            self.require_stopped(payload["containers"]["coding-worker"])
            payload["stopped_at"] = datetime.now(UTC).isoformat()
            self.phase(event, "worker_stopped")
        if event["phase"] == "worker_stopped":
            self.require_stopped(payload["containers"]["coding-worker"])
            payload["proof"] = str(self.write_proof(event))
            self.phase(event, "proof_written")
        if event["phase"] == "proof_written":
            self.units("start", "coding-worker")
            self.current_intent(event)
            self.phase(event, "diagnostics_started")
        if event["phase"] == "diagnostics_started":
            if not self.quarantined(event):
                for service in _SERVICES[:2]:
                    self.require_stopped(payload["containers"][service])
                with suppress(
                    OSError, ValueError, RuntimeError, subprocess.TimeoutExpired
                ):
                    self.compose(
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--volume",
                        payload["proof"] + ":/run/agentd/quarantine-stop.json:ro",
                        "coding-controller",
                        "github",
                        "--config",
                        "/etc/agentd/controller.json",
                        "quarantine",
                        payload["run_id"],
                        "--actor",
                        payload["actor"],
                        "--event-id",
                        event["event_id"],
                        "--stop-proof",
                        "/run/agentd/quarantine-stop.json",
                        timeout=150,
                    )
            if not self.quarantined(event):
                raise AbandonBlocked("quarantine_response_unconfirmed")
            self.phase(event, "quarantined")
        if event["phase"] in {"quarantined", "restoring"}:
            self.phase(event, "restoring")
            self.units("start", "coding-controller", "coding-publisher")
            self.current_intent(event)
            if any(
                (self.inspect(payload["containers"][service]).get("State") or {}).get(
                    "Running"
                )
                is not True
                for service in _SERVICES
            ):
                raise AbandonBlocked("quarantine_service_restart_unconfirmed")
            self.phase(event, "complete")

    def report(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        summary = {
            "event_id": event["event_id"],
            "run_id": payload.get("run_id"),
            "phase": event["phase"],
            "error": event.get("error"),
            "metering_unknown": event["phase"] == "complete",
            "retry_required": event.get("attempts", 0) >= self.maximum_attempts
            and event["phase"] not in _DONE,
        }
        marker = (
            "<!-- agentd:host-abandon:"
            + hashlib.sha256(event["event_id"].encode()).hexdigest()
            + " -->"
        )
        body = marker + "\nAgentd abandonment: `" + event["phase"] + "`."
        if payload.get("run_id"):
            body += " Run `" + payload["run_id"] + "`."
        if event.get("error"):
            body += " Gate: `" + event["error"] + "`."
        body += "\n\nPhysical retirement does not establish metered completion."
        if event["phase"] == "complete":
            body += " Unknown usage and the quota reservation are retained."
        if (
            event.get("attempts", 0) >= self.maximum_attempts
            and event["phase"] not in _DONE
        ):
            body += (
                " Automatic attempts are exhausted. To authorize another bounded "
                "retry, post a new `/agentd abandon "
                + payload["run_id"]
                + "` comment on the operations issue."
            )
        if event.get("reported_body") == body:
            return
        self.watcher.record("abandon_" + event["phase"], abandon=summary)
        number = payload.get("source_issue", payload["subject_number"])
        account = self.watcher.status_api("user")
        identity = account.get("id")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise AbandonBlocked("abandon_report_account_invalid")
        comments = self.watcher.status_api(
            f"repos/{REPOSITORY}/issues/{number}/comments", paginated=True
        )
        owned = [
            row
            for row in comments
            if (row.get("user") or {}).get("id") == identity
            and isinstance(row.get("body"), str)
            and row["body"].startswith(marker + "\n")
        ]
        if len(owned) > 1:
            raise AbandonBlocked("abandon_report_ambiguous")
        selected = owned[0] if owned else None
        if selected and selected["body"] == body:
            response = selected
        else:
            if selected is None:
                if payload.get("report_creation_started"):
                    raise AbandonBlocked("abandon_report_creation_unconfirmed")
                payload["report_creation_started"] = True
                self.save(event)
            response = self.watcher.status_api(
                f"repos/{REPOSITORY}/issues/comments/{selected['id']}"
                if selected
                else f"repos/{REPOSITORY}/issues/{number}/comments",
                method="PATCH" if selected else "POST",
                data={"body": body},
            )
        if (
            not isinstance(response.get("id"), int)
            or isinstance(response["id"], bool)
            or response["id"] <= 0
        ):
            raise AbandonBlocked("abandon_report_identity_invalid")
        event.update(report_id=response["id"], reported_body=body)
        self.save(event)

    def tick(self) -> bool:
        repository = self.watcher.github(f"repos/{REPOSITORY}")
        if (
            repository.get("id") != REPOSITORY_ID
            or repository.get("full_name") != REPOSITORY
        ):
            raise AbandonBlocked("abandon_repository_identity_changed")
        events = self.events()
        pending = [event for event in events if event["phase"] not in _DONE]
        exhausted = bool(
            pending and pending[0].get("attempts", 0) >= self.maximum_attempts
        )
        if not pending or exhausted:
            query = urlencode({"since": self.config["activated_at"], "per_page": 100})
            comments = self.watcher.github(
                f"repos/{REPOSITORY}/issues/comments?{query}", paginated=True
            )
            recorded = {event["event_id"] for event in events}
            for comment in sorted(comments, key=lambda row: row.get("id", 0)):
                command = fresh_abandon(
                    comment,
                    actors=self.watcher.actors,
                    actor_ids=self.watcher.actor_ids,
                    activated_at=self.config["activated_at"],
                )
                match = re.fullmatch(
                    f"https://api.github.com/repos/{REPOSITORY}/issues/([1-9][0-9]*)",
                    comment.get("issue_url", ""),
                )
                event_id = f"github:{REPOSITORY_ID}:comment:{comment.get('id')}"
                if command is None or match is None or event_id in recorded:
                    continue
                actor, requested = command
                subject = int(match.group(1))
                if exhausted:
                    original = pending[0]
                    if (
                        subject != self.watcher.config.get("status_issue_number")
                        or requested != original["payload"]["run_id"]
                    ):
                        continue
                    # A new human event authorizes another bounded burst. The
                    # original stop/worker-RPC event and proof stay immutable.
                    authorization = {
                        "subject_number": subject,
                        "actor": actor,
                        "actor_id": comment["user"]["id"],
                        "requested_run_id": requested,
                        "comment_id": comment["id"],
                        "comment_node_id": comment["node_id"],
                    }
                    retry = {
                        "event_id": event_id,
                        "phase": "retry_authorized",
                        "attempts": 0,
                        "payload": {
                            **authorization,
                            "source_issue": original["payload"]["source_issue"],
                            "run_id": requested,
                            "origin_event_id": original["event_id"],
                            "origin_actor": original["payload"]["actor"],
                        },
                    }
                    original["payload"]["authorization"] = authorization
                    self.refresh_command(original)
                    original.update(attempts=0, error=None)
                    self.save_many([retry, original])
                    events.append(retry)
                    break
                event = {
                    "event_id": event_id,
                    "phase": "accepted",
                    "attempts": 0,
                    "payload": {
                        "subject_number": subject,
                        "actor": actor,
                        "actor_id": comment["user"]["id"],
                        "requested_run_id": requested,
                        "comment_id": comment["id"],
                        "comment_node_id": comment["node_id"],
                    },
                }
                try:
                    target = self.resolve_run(subject, requested)
                    source = self.watcher.github(
                        f"repos/{REPOSITORY}/issues/{target['source_issue']}"
                    )
                    if (
                        source.get("node_id") != target["source_node_id"]
                        or source.get("number") != target["source_issue"]
                        or source.get("repository_url")
                        != f"https://api.github.com/repos/{REPOSITORY}"
                    ):
                        raise AbandonBlocked("abandon_source_issue_identity_changed")
                    if subject != target[
                        "source_issue"
                    ] and subject != self.watcher.config.get("status_issue_number"):
                        pull = self.watcher.github(
                            f"repos/{REPOSITORY}/pulls/{subject}"
                        )
                        if ((pull.get("base") or {}).get("repo") or {}).get(
                            "id"
                        ) != REPOSITORY_ID:
                            raise AbandonBlocked(
                                "abandon_publication_repository_changed"
                            )
                    event["payload"].update(
                        target,
                        containers=self.containers(),
                        release=str(self.current.resolve()),
                    )
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    RuntimeError,
                    sqlite3.Error,
                    subprocess.TimeoutExpired,
                ) as error:
                    event.update(
                        phase="rejected",
                        error=str(error)
                        if isinstance(error, AbandonBlocked)
                        else type(error).__name__,
                    )
                self.save(event)
                events.append(event)
                if event["phase"] == "accepted":
                    pending.append(event)
                    break  # Only one accepted worker stop intent at a time.
        if pending:
            event = pending[0]
            if event.get("attempts", 0) < self.maximum_attempts:
                event["attempts"] = event.get("attempts", 0) + 1
                self.save(event)
                try:
                    self.execute(event)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    RuntimeError,
                    sqlite3.Error,
                    subprocess.TimeoutExpired,
                ) as error:
                    event["error"] = (
                        str(error)
                        if isinstance(error, AbandonBlocked)
                        else type(error).__name__
                    )
                    if isinstance(error, AbandonBlocked) and event["phase"] in {
                        "controllers_stopping",
                        "controllers_stopped",
                    }:
                        # Before a worker stop has been issued, a changed
                        # command/known terminal result can safely decline and
                        # restore only the exact original service supervisors.
                        with suppress(
                            OSError, ValueError, RuntimeError, subprocess.TimeoutExpired
                        ):
                            self.current_intent(event)
                            self.units("start", "coding-controller", "coding-publisher")
                            if all(
                                (
                                    self.inspect(
                                        event["payload"]["containers"][service]
                                    ).get("State")
                                    or {}
                                ).get("Running")
                                is True
                                for service in _SERVICES[:2]
                            ):
                                event["phase"] = "declined"
                    self.save(event)
            events = self.events()
        for event in events:
            # A pending durable report can retry without issuing another stop.
            with suppress(
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RuntimeError,
                subprocess.TimeoutExpired,
            ):
                self.report(event)
        return any(event["phase"] not in _DONE for event in events)
