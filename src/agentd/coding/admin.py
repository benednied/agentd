"""Durable local operator requests executed by the controller lock owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from agentd.coordinator import LifecycleError, SchedulerCoordinator
from agentd.domain.models import utc_now


class CodingAdminStore:
    """Local database access is operator authority; this is not a model tool."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS coding_admin_requests ("
                "id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "created_at TEXT NOT NULL, result TEXT, last_error TEXT)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        request_id = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO coding_admin_requests "
                "(id, payload, created_at) VALUES (?, ?, ?)",
                (request_id, encoded, utc_now().isoformat()),
            )
        return self.get(request_id)

    def get(self, request_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, result, last_error FROM coding_admin_requests "
                "WHERE id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown coding administrative request")
        return {
            "request_id": request_id,
            "request": json.loads(row[0]),
            "result": json.loads(row[1]) if row[1] else None,
            "last_error": row[2],
            "status": "finished" if row[1] else "queued",
        }

    async def apply_pending(self, coordinator: SchedulerCoordinator) -> None:
        """Only called by the controller holding its exclusive lifecycle lock."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM coding_admin_requests WHERE result IS NULL "
                "ORDER BY created_at, id LIMIT 4"
            ).fetchall()
        for request_id, encoded in rows:
            payload = json.loads(encoded)
            try:
                job = await coordinator.reset_coding_budget(**payload)
                result = {"state": job.state.value, "job_id": job.id, "ok": True}
            except (LifecycleError, ValueError) as error:
                result = {"ok": False, "error": str(error)}
            except Exception as error:
                # Preserve unknown outcomes for the exact same idempotent retry.
                # Transport failures do not authorize a different attempt/reset.
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE coding_admin_requests SET last_error = ? WHERE id = ?",
                        (type(error).__name__, request_id),
                    )
                continue
            with self._connect() as connection:
                connection.execute(
                    "UPDATE coding_admin_requests SET result = ?, last_error = NULL "
                    "WHERE id = ? AND result IS NULL",
                    (json.dumps(result, sort_keys=True), request_id),
                )
