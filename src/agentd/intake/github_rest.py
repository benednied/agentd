"""Authenticated REST polling, with a budget shared by trusted processes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


class GitHubRateLimited(RuntimeError):
    def __init__(self, kind: str, retry_at: float) -> None:
        self.kind, self.retry_at = kind, retry_at
        super().__init__(f"GitHub {kind} rate limit until Unix time {retry_at:.0f}")


class GitHubREST:
    """Use gh's credentials and keep validators private to this client."""

    def __init__(
        self,
        *,
        database: Path | None = None,
        component: str = "admin",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database, self.component, self.clock = database, component, clock
        self._cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._deferred: GitHubRateLimited | None = None
        if database is not None:
            with closing(sqlite3.connect(database, timeout=5)) as db:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS budget (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        remaining INTEGER, reset_at INTEGER, observed_at REAL,
                        kind TEXT, retry_at REAL NOT NULL DEFAULT 0,
                        secondary_failures INTEGER NOT NULL DEFAULT 0);
                    INSERT OR IGNORE INTO budget (id) VALUES (1);
                    CREATE TABLE IF NOT EXISTS requests (
                        component TEXT, operation TEXT, outcome TEXT,
                        count INTEGER NOT NULL,
                        PRIMARY KEY (component, operation, outcome));
                    COMMIT;
                    """)

    def check_budget(self) -> None:
        now = self.clock()
        if self.database is not None:
            with closing(sqlite3.connect(self.database, timeout=5)) as db:
                kind, retry_at = db.execute(
                    "SELECT kind, retry_at FROM budget WHERE id = 1"
                ).fetchone()
            if retry_at > now:
                raise GitHubRateLimited(kind, retry_at)
        elif self._deferred is not None and self._deferred.retry_at > now:
            raise self._deferred

    @staticmethod
    def diagnostics(database: Path, *, now: float) -> dict[str, Any]:
        if not database.is_file():
            return {}
        with closing(sqlite3.connect(database, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute("SELECT * FROM budget WHERE id = 1").fetchone())
            requests = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM requests ORDER BY component, operation, outcome"
                )
            ]
        return {
            "remaining": row["remaining"],
            "reset_at": row["reset_at"],
            "observed_at": row["observed_at"],
            "backoff": {"kind": row["kind"], "retry_at": row["retry_at"]}
            if row["retry_at"] > now
            else None,
            "requests": requests,
        }

    def request(
        self,
        operation: str,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        conditional: bool = False,
        timeout: float = 30,
    ) -> Any:
        self.check_budget()
        cached = self._cache.get(endpoint) if conditional and method == "GET" else None
        command = ["gh", "api", "--include", "--method", method, endpoint]
        if cached:
            command.extend(("--header", f"If-None-Match: {cached[0]}"))
        if payload is not None:
            command.extend(("--input", "-"))
        try:
            result = subprocess.run(
                command,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._observe(operation, "transport_error", {})
            raise
        status, headers, body = _response(result.stdout)
        remaining = _integer(headers.get("x-ratelimit-remaining"))
        message = (body + result.stderr).lower() if status in (403, 429) else ""
        kind = None
        if status in (403, 429):
            if remaining == 0:
                kind = "primary"
            elif status == 429 or "retry-after" in headers or "rate limit" in message:
                kind = "secondary"
        outcome = kind or (str(status) if status else "cli_error")
        deferred = self._observe(operation, outcome, headers, kind=kind)
        if kind and deferred is not None:
            raise deferred
        if status == 304 and cached:
            self._cache.move_to_end(endpoint)
            return json.loads(cached[1])
        result.check_returncode()
        if status is None or not 200 <= status < 300:
            raise ValueError("GitHub REST response is missing a successful HTTP status")
        value = json.loads(body) if body.strip() else None
        if conditional and method == "GET":
            self._cache.pop(endpoint, None)
            if headers.get("etag") and len(body) <= 1_000_000:
                self._cache[endpoint] = (headers["etag"], body)
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
        return value

    def _observe(
        self,
        operation: str,
        outcome: str,
        headers: dict[str, str],
        *,
        kind: str | None = None,
    ) -> GitHubRateLimited | None:
        now = self.clock()
        remaining = _integer(headers.get("x-ratelimit-remaining"))
        reset = _integer(headers.get("x-ratelimit-reset"))
        retry = _retry_at(headers.get("retry-after"), now)

        def deadline(failures: int) -> float:
            if kind == "secondary":
                return retry or now + min(60 * 2 ** min(failures, 6), 3600)
            if reset is not None and reset > now:
                return max(float(reset), retry or 0)
            return retry or now + 60

        # A successful response can consume the last request in the window.
        # Keep that response, but block the next call in either process.
        exhausted = remaining == 0 and outcome.isdecimal() and int(outcome) < 400
        if self.database is None:
            if kind or exhausted:
                self._deferred = GitHubRateLimited(kind or "primary", deadline(0))
                return self._deferred
            return None
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.execute(
                "INSERT INTO requests VALUES (?, ?, ?, 1) "
                "ON CONFLICT(component, operation, outcome) "
                "DO UPDATE SET count=count+1",
                (self.component, operation, outcome),
            )
            if remaining is not None and reset is not None:
                db.execute(
                    "UPDATE budget SET remaining=?, reset_at=?, observed_at=? "
                    "WHERE reset_at IS NULL OR reset_at < ? OR "
                    "(reset_at = ? AND remaining >= ?)",
                    (remaining, reset, now, reset, reset, remaining),
                )
            if kind or exhausted:
                failures = db.execute(
                    "SELECT secondary_failures FROM budget WHERE id = 1"
                ).fetchone()[0]
                retry_at = deadline(failures)
                db.execute(
                    "UPDATE budget SET kind=?, retry_at=?, "
                    "secondary_failures=secondary_failures+? WHERE retry_at <= ?",
                    (kind or "primary", retry_at, int(kind == "secondary"), retry_at),
                )
                active_kind, active_retry_at = db.execute(
                    "SELECT kind, retry_at FROM budget WHERE id = 1"
                ).fetchone()
                return GitHubRateLimited(active_kind, active_retry_at)
            if outcome.isdecimal() and int(outcome) < 400:
                db.execute(
                    "UPDATE budget SET secondary_failures=0 WHERE retry_at <= ?",
                    (now,),
                )
        return None


def _response(stdout: str) -> tuple[int | None, dict[str, str], str]:
    head, separator, body = stdout.partition("\n\n")
    lines = head.splitlines()
    if not separator or not lines or not lines[0].startswith("HTTP/"):
        return None, {}, stdout
    status_line = lines[0].split(maxsplit=2)
    if len(status_line) < 2 or not status_line[1].isdecimal():
        return None, {}, stdout
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.lower()] = value.strip()
    return int(status_line[1]), headers, body


def _integer(value: str | None) -> int | None:
    return int(value) if value and value.isdecimal() else None


def _retry_at(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    delay = _integer(value)
    if delay is not None:
        return now + max(1, delay)
    try:
        date = parsedate_to_datetime(value)
        return max(now + 1, date.replace(tzinfo=date.tzinfo or UTC).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None
