"""Durable, tiny operation journal for worker request idempotency."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from agentd.workers.errors import WorkerJournalConflictError, WorkerProtocolError
from agentd.workers.remote_protocol import MAX_STRING_SIZE, canonical_json


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One completed or in-flight operation retained across worker restarts."""

    node_id: str
    session_epoch: str
    request_id: str
    action: str
    payload_hash: str
    status: str
    response: dict[str, Any] | None
    sequence: int | None
    created_at: float
    reserved_here: bool = False


class OperationJournal:
    """SQLite-backed operation deduplication and event sequence allocator.

    A request is reserved before the side effect starts.  If a worker dies in
    that interval, a retry sees ``pending`` and fails closed instead of running
    the side effect twice.  Completed requests return their exact stored
    response, including an error response.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        node_id: str,
        session_epoch: str,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._validate_identifier(node_id, "node_id")
        self._validate_identifier(session_epoch, "session_epoch")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = str(path)
        self.node_id = node_id
        self.session_epoch = session_epoch
        self._clock = clock
        if self.path != ":memory:":
            resolved_path = Path(self.path).expanduser().resolve()
            self.path = str(resolved_path)
            resolved_path.parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            if self.path != ":memory:":
                os.chmod(self.path, 0o600)
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_operations (
                    node_id TEXT NOT NULL,
                    session_epoch TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response TEXT,
                    sequence INTEGER,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(node_id, session_epoch, request_id, action)
                );
                CREATE TABLE IF NOT EXISTS worker_event_counters (
                    node_id TEXT NOT NULL,
                    session_epoch TEXT NOT NULL,
                    next_sequence INTEGER NOT NULL,
                    PRIMARY KEY(node_id, session_epoch)
                );
                CREATE TABLE IF NOT EXISTS worker_run_claims (
                    node_id TEXT NOT NULL,
                    session_epoch TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    start_hash TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'claimed',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(node_id, session_epoch, run_id)
                );
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(worker_run_claims)"
                )
            }
            if "state" not in columns:
                # Existing journals predate durable claim state. Such rows
                # are conservatively treated as unresolved claims.
                self._connection.execute(
                    "ALTER TABLE worker_run_claims ADD COLUMN state TEXT "
                    "NOT NULL DEFAULT 'claimed'"
                )
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> OperationJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def lookup(
        self,
        *,
        request_id: str,
        action: str,
        payload_hash: str,
    ) -> JournalEntry | None:
        self._validate_key(request_id, action, payload_hash)
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            row = self._connection.execute(
                "SELECT * FROM worker_operations WHERE node_id = ? "
                "AND session_epoch = ? AND request_id = ? AND action = ?",
                (self.node_id, self.session_epoch, request_id, action),
            ).fetchone()
        if row is None:
            return None
        entry = self._entry(row)
        if entry.payload_hash != payload_hash:
            raise WorkerJournalConflictError(
                f"request {request_id!r} was reused with a different payload"
            )
        return entry

    # ``get`` is intentionally an alias for callers that treat the journal as
    # a key/value store; both names retain the same payload-hash conflict check.
    get = lookup

    def claim_run(self, *, run_id: str, start_hash: str) -> bool:
        """Claim a durable run id before invoking a start side effect.

        The claim deliberately survives failed starts and worker restarts.  A
        new request id must never cause an indeterminate earlier start to run
        again.  ``True`` means this journal connection created the claim;
        ``False`` means an identical start was already claimed in this worker
        session.  A different start payload for the same run id is a conflict.
        """

        self._validate_identifier(run_id, "run_id")
        self._validate_payload_hash(start_hash, "start_hash")
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT start_hash FROM worker_run_claims WHERE node_id = ? "
                    "AND session_epoch = ? AND run_id = ?",
                    (self.node_id, self.session_epoch, run_id),
                ).fetchone()
                if row is not None:
                    if str(row["start_hash"]) != start_hash:
                        raise WorkerJournalConflictError(
                            f"run {run_id!r} was reused with a different start"
                        )
                    self._connection.execute("COMMIT")
                    return False
                self._connection.execute(
                    "INSERT INTO worker_run_claims(node_id, session_epoch, "
                    "run_id, start_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        self.node_id,
                        self.session_epoch,
                        run_id,
                        start_hash,
                        float(self._clock()),
                    ),
                )
                self._connection.execute("COMMIT")
                return True
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def run_claim_state(self, *, run_id: str) -> str | None:
        """Return the durable state of a claimed run, if one exists."""

        self._validate_identifier(run_id, "run_id")
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            row = self._connection.execute(
                "SELECT state FROM worker_run_claims WHERE node_id = ? "
                "AND session_epoch = ? AND run_id = ?",
                (self.node_id, self.session_epoch, run_id),
            ).fetchone()
        if row is None:
            return None
        state = str(row["state"])
        if state not in {"claimed", "started"}:
            raise WorkerProtocolError("worker run claim state is invalid")
        return state

    def mark_run_started(self, *, run_id: str, start_hash: str) -> None:
        """Record that the worker created a run for a durable claim."""

        self._set_run_claim_state(run_id, start_hash, "started")

    def _set_run_claim_state(
        self,
        run_id: str,
        start_hash: str,
        state: str,
    ) -> None:
        self._validate_identifier(run_id, "run_id")
        self._validate_payload_hash(start_hash, "start_hash")
        if state != "started":
            raise ValueError("invalid worker run claim state")
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            row = self._connection.execute(
                "SELECT start_hash FROM worker_run_claims WHERE node_id = ? "
                "AND session_epoch = ? AND run_id = ?",
                (self.node_id, self.session_epoch, run_id),
            ).fetchone()
            if row is None:
                raise WorkerProtocolError("worker run claim does not exist")
            if str(row["start_hash"]) != start_hash:
                raise WorkerJournalConflictError(
                    f"run {run_id!r} was reused with a different start"
                )
            self._connection.execute(
                "UPDATE worker_run_claims SET state = ? WHERE node_id = ? "
                "AND session_epoch = ? AND run_id = ?",
                (state, self.node_id, self.session_epoch, run_id),
            )

    def begin(
        self,
        *,
        request_id: str,
        action: str,
        payload_hash: str,
    ) -> JournalEntry:
        """Reserve an operation or return the exact existing journal entry."""

        self._validate_key(request_id, action, payload_hash)
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM worker_operations WHERE node_id = ? "
                    "AND session_epoch = ? AND request_id = ? AND action = ?",
                    (self.node_id, self.session_epoch, request_id, action),
                ).fetchone()
                if row is not None:
                    entry = self._entry(row)
                    if entry.payload_hash != payload_hash:
                        raise WorkerJournalConflictError(
                            f"request {request_id!r} was reused with a different "
                            "payload"
                        )
                    self._connection.execute("COMMIT")
                    return entry
                now = float(self._clock())
                self._connection.execute(
                    "INSERT INTO worker_operations("
                    "node_id, session_epoch, request_id, action, payload_hash, "
                    "status, response, sequence, created_at"
                    ") VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)",
                    (
                        self.node_id,
                        self.session_epoch,
                        request_id,
                        action,
                        payload_hash,
                        now,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return JournalEntry(
            node_id=self.node_id,
            session_epoch=self.session_epoch,
            request_id=request_id,
            action=action,
            payload_hash=payload_hash,
            status="pending",
            response=None,
            sequence=None,
            created_at=now,
            reserved_here=True,
        )

    def complete(
        self,
        *,
        request_id: str,
        action: str,
        payload_hash: str,
        response: dict[str, Any],
        sequence: int,
    ) -> JournalEntry:
        """Persist the exact response of a previously reserved operation."""

        self._validate_key(request_id, action, payload_hash)
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        canonical_json(response)
        encoded = json.dumps(
            response,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM worker_operations WHERE node_id = ? "
                    "AND session_epoch = ? AND request_id = ? AND action = ?",
                    (self.node_id, self.session_epoch, request_id, action),
                ).fetchone()
                if row is None:
                    raise WorkerProtocolError("operation was not reserved")
                existing = self._entry(row)
                if existing.payload_hash != payload_hash:
                    raise WorkerJournalConflictError(
                        f"request {request_id!r} was reused with a different payload"
                    )
                if existing.status == "completed":
                    if existing.response != response or existing.sequence != sequence:
                        raise WorkerJournalConflictError(
                            f"request {request_id!r} already has a different response"
                        )
                    self._connection.execute("COMMIT")
                    return existing
                self._connection.execute(
                    "UPDATE worker_operations SET status = 'completed', "
                    "response = ?, sequence = ? WHERE node_id = ? "
                    "AND session_epoch = ? AND request_id = ? AND action = ?",
                    (
                        encoded,
                        sequence,
                        self.node_id,
                        self.session_epoch,
                        request_id,
                        action,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return JournalEntry(
            node_id=self.node_id,
            session_epoch=self.session_epoch,
            request_id=request_id,
            action=action,
            payload_hash=payload_hash,
            status="completed",
            response=dict(response),
            sequence=sequence,
            created_at=existing.created_at,
        )

    def next_sequence(self) -> int:
        """Allocate a monotone server event sequence surviving restarts."""

        with self._lock:
            if self._connection is None:
                raise WorkerProtocolError("operation journal is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT next_sequence FROM worker_event_counters "
                    "WHERE node_id = ? AND session_epoch = ?",
                    (self.node_id, self.session_epoch),
                ).fetchone()
                sequence = int(row[0]) if row is not None else 0
                self._connection.execute(
                    "INSERT INTO worker_event_counters(node_id, session_epoch, "
                    "next_sequence) VALUES (?, ?, ?) "
                    "ON CONFLICT(node_id, session_epoch) DO UPDATE SET "
                    "next_sequence = excluded.next_sequence",
                    (self.node_id, self.session_epoch, sequence + 1),
                )
                self._connection.execute("COMMIT")
                return sequence
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def _entry(self, row: sqlite3.Row) -> JournalEntry:
        response = row["response"]
        try:
            decoded = json.loads(response) if response is not None else None
        except (TypeError, ValueError) as error:
            raise WorkerProtocolError("journal response is invalid") from error
        if decoded is not None and not isinstance(decoded, dict):
            raise WorkerProtocolError("journal response is not an object")
        return JournalEntry(
            node_id=str(row["node_id"]),
            session_epoch=str(row["session_epoch"]),
            request_id=str(row["request_id"]),
            action=str(row["action"]),
            payload_hash=str(row["payload_hash"]),
            status=str(row["status"]),
            response=decoded,
            sequence=(int(row["sequence"]) if row["sequence"] is not None else None),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _validate_identifier(value: str, name: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_STRING_SIZE
            or "\x00" in value
        ):
            raise ValueError(f"{name} is empty or exceeds the string limit")

    @classmethod
    def _validate_key(
        cls,
        request_id: str,
        action: str,
        payload_hash_value: str,
    ) -> None:
        cls._validate_identifier(request_id, "request_id")
        cls._validate_identifier(action, "action")
        cls._validate_payload_hash(payload_hash_value, "payload_hash")

    @staticmethod
    def _validate_payload_hash(value: str, name: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{name} must be a SHA-256 hex digest")


__all__ = ["JournalEntry", "OperationJournal"]
