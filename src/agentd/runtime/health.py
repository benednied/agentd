"""Durable process pulses and pure liveness checks, separate from admission.

An idle queue, quota pressure, and dependencies can all be healthy waits. A
kernel ownership lock alone cannot prove that a service still polls its queue.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any

from agentd.domain.models import WorkerHeartbeat, utc_now

_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_EXPECTED_WAITS = frozenset(
    {"waiting_quota", "waiting_dependencies", "waiting_review", "idle", "draining"}
)


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat:
    role: str
    state: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if _LABEL.fullmatch(self.role) is None or _LABEL.fullmatch(self.state) is None:
            raise ValueError("Runtime heartbeat role/state must be compact labels")
        if self.observed_at.utcoffset() != timedelta(0):
            raise ValueError("Runtime heartbeat observation must use UTC")


class RuntimeHealthStore:
    """Add a small role heartbeat table to the existing controller database.

    This table never changes the state-store schema version or execution rows.
    It stores only time and compact role/state labels, with no arbitrary output.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("Runtime health busy timeout must be non-negative")
        self._lock = RLock()
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_health_heartbeats ("
                "role TEXT PRIMARY KEY, state TEXT NOT NULL, observed_at TEXT NOT NULL)"
            )
        except BaseException:
            self._connection.close()
            raise

    def pulse(
        self, role: str, *, state: str = "polling", at: datetime | None = None
    ) -> RuntimeHeartbeat:
        heartbeat = RuntimeHeartbeat(role, state, at if at is not None else utc_now())
        with self._lock:
            self._connection.execute(
                "INSERT INTO runtime_health_heartbeats(role, state, observed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(role) DO UPDATE SET "
                "state = excluded.state, observed_at = excluded.observed_at "
                "WHERE excluded.observed_at >= runtime_health_heartbeats.observed_at",
                (heartbeat.role, heartbeat.state, heartbeat.observed_at.isoformat()),
            )
            latest = self.latest(role)
            assert latest is not None
            return latest

    def latest(self, role: str) -> RuntimeHeartbeat | None:
        if _LABEL.fullmatch(role) is None:
            raise ValueError("Runtime heartbeat role must be a compact label")
        with self._lock:
            row = self._connection.execute(
                "SELECT role, state, observed_at FROM runtime_health_heartbeats "
                "WHERE role = ?",
                (role,),
            ).fetchone()
        return (
            RuntimeHeartbeat(
                row["role"], row["state"], datetime.fromisoformat(row["observed_at"])
            )
            if row is not None
            else None
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> RuntimeHealthStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _age_seconds(observed_at: datetime, at: datetime) -> float | None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Liveness observation time must be timezone aware")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return None
    return (at - observed_at).total_seconds()


def _validate_staleness(seconds: float) -> None:
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError("Liveness stale threshold must be finite and positive")


def runtime_liveness(
    heartbeat: RuntimeHeartbeat | None,
    *,
    owner_live: bool,
    at: datetime | None = None,
    stale_after_seconds: float = 180,
) -> dict[str, Any]:
    """Require fresh polling evidence and live kernel ownership for a role.

    This does not assert provider availability, worker ownership resolution, or
    publication readiness. Those are separate gates and must remain visible.
    """
    _validate_staleness(stale_after_seconds)
    now = at if at is not None else utc_now()
    age = _age_seconds(heartbeat.observed_at, now) if heartbeat is not None else None
    if not owner_live:
        reason = "owner_not_live"
    elif heartbeat is None:
        reason = "heartbeat_missing"
    elif age is None or age < 0:
        reason = "heartbeat_future_or_invalid"
    elif age > stale_after_seconds:
        reason = "heartbeat_stale"
    else:
        reason = "heartbeat_fresh"
    return {
        "live": reason == "heartbeat_fresh",
        "reason": reason,
        "state": heartbeat.state if heartbeat is not None else None,
        "observed_at": heartbeat.observed_at.isoformat()
        if heartbeat is not None
        else None,
        "age_seconds": age,
        "expected_wait": reason == "heartbeat_fresh"
        and heartbeat is not None
        and heartbeat.state in _EXPECTED_WAITS,
    }


def worker_heartbeat_ready(
    heartbeat: WorkerHeartbeat | None,
    *,
    at: datetime | None = None,
    stale_after_seconds: float = 45,
    expected_epoch: str | None = None,
    required_driver: str = "remote-coding",
) -> bool:
    """Bound readiness by authenticated time, configured epoch, and driver."""
    _validate_staleness(stale_after_seconds)
    if heartbeat is None:
        return False
    if (
        expected_epoch is not None and heartbeat.session_epoch != expected_epoch
    ) or required_driver not in heartbeat.drivers:
        return False
    now = at if at is not None else utc_now()
    age = _age_seconds(heartbeat.observed_at, now)
    return age is not None and 0 <= age <= stale_after_seconds
