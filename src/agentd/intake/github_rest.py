"""Authenticated REST polling, with a budget shared by trusted processes."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from contextlib import closing
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from agentd.observability import event_logger

_REQUEST_ID = re.compile(r"[0-9A-Fa-f]{1,16}(?::[0-9A-Fa-f]{1,16}){2,5}\Z")
_SUMMARY_SECONDS = 15 * 60
_FAILURE_LOG_SECONDS = 5 * 60


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
        repository: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database, self.component, self.clock = database, component, clock
        self.repository = repository
        self._cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._deferred: GitHubRateLimited | None = None
        self._reported_backoff: tuple[str, float] | None = None
        self._last_failure_log: dict[tuple[str, str, int | None], float] = {}
        self._window_start = clock()
        self._counts: dict[str, Counter[str]] = {}
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

    def check_budget(self, operation: str = "github_rest") -> None:
        now = self.clock()
        if self.database is not None:
            with closing(sqlite3.connect(self.database, timeout=5)) as db:
                kind, retry_at = db.execute(
                    "SELECT kind, retry_at FROM budget WHERE id = 1"
                ).fetchone()
            if retry_at > now:
                self._raise_deferred(operation, GitHubRateLimited(kind, retry_at))
        elif self._deferred is not None and self._deferred.retry_at > now:
            self._raise_deferred(operation, self._deferred)

    def _raise_deferred(self, operation: str, error: GitHubRateLimited) -> None:
        backoff = (error.kind, error.retry_at)
        if self._reported_backoff != backoff:
            self._reported_backoff = backoff
            event_logger(
                component=self.component,
                repository=self.repository,
                operation=operation,
                rate_limit_kind=error.kind,
                retry_at=error.retry_at,
            ).warning("github_rest_deferred")
        raise error

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
        self.check_budget(operation)
        cached = self._cache.get(endpoint) if conditional and method == "GET" else None
        command = ["gh", "api", "--include", "--method", method, endpoint]
        if cached:
            command.extend(("--header", f"If-None-Match: {cached[0]}"))
        if payload is not None:
            command.extend(("--input", "-"))
        started = time.monotonic()
        headers: dict[str, str] = {}
        status = None
        failure = None
        backoff = None
        try:
            result = subprocess.run(
                command,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            status, headers, body = _response(result.stdout)
            kind = _rate_limit_kind(status, headers, body, result.stderr)
            outcome = kind or (str(status) if status else "cli_error")
            backoff = self._observe(operation, outcome, headers, kind=kind)
            if kind and backoff is not None:
                failure = "rate_limited"
                raise backoff
            if status == 304 and cached:
                self._cache.move_to_end(endpoint)
                return json.loads(cached[1])
            if result.returncode:
                failure = _cli_failure(status, result.stderr)
                result.check_returncode()
            if status is None or not 200 <= status < 300:
                failure = "unexpected_response"
                raise ValueError(
                    "GitHub REST response is missing a successful HTTP status"
                )
            value = json.loads(body) if body.strip() else None
            if conditional and method == "GET":
                self._cache.pop(endpoint, None)
                if headers.get("etag") and len(body) <= 1_000_000:
                    self._cache[endpoint] = (headers["etag"], body)
                    if len(self._cache) > 256:
                        self._cache.popitem(last=False)
            return value
        except (OSError, subprocess.TimeoutExpired) as error:
            failure = type(error).__name__
            self._observe(operation, "transport_error", {})
            raise
        except (json.JSONDecodeError, sqlite3.Error) as error:
            failure = type(error).__name__
            raise
        finally:
            self._log_attempt(
                operation,
                method,
                status,
                headers,
                conditional_sent=bool(cached),
                duration_ms=round((time.monotonic() - started) * 1000),
                failure=failure,
                backoff=backoff,
            )

    def _log_attempt(
        self,
        operation: str,
        method: str,
        status: int | None,
        headers: dict[str, str],
        *,
        conditional_sent: bool,
        duration_ms: int,
        failure: str | None,
        backoff: GitHubRateLimited | None,
    ) -> None:
        now = self.clock()
        request_id = headers.get("x-github-request-id", "")
        quota = {
            "rate_limit": _integer(headers.get("x-ratelimit-limit")),
            "rate_remaining": _integer(headers.get("x-ratelimit-remaining")),
            "rate_reset_at": _integer(headers.get("x-ratelimit-reset")),
        }
        log = event_logger(
            component=self.component,
            repository=self.repository,
            operation=operation,
            http_method=method,
            http_status=status,
            conditional_sent=conditional_sent,
            duration_ms=duration_ms,
            error_type=failure,
            github_request_id=request_id if _REQUEST_ID.fullmatch(request_id) else None,
            retry_after_at=_retry_at(headers.get("retry-after"), now),
            **quota,
        )
        log.debug("github_rest_request")
        if backoff is not None:
            observed = (backoff.kind, backoff.retry_at)
            if self._reported_backoff != observed:
                self._reported_backoff = observed
                log.bind(
                    rate_limit_kind=backoff.kind, retry_at=backoff.retry_at
                ).warning("github_rest_backoff_set")
        if failure is not None and failure != "rate_limited":
            key = (operation, failure, status)
            last_logged = self._last_failure_log.get(key)
            if last_logged is None or now - last_logged >= _FAILURE_LOG_SECONDS:
                self._last_failure_log[key] = now
                log.warning("github_rest_request_failed")
        if failure is None and self._reported_backoff is not None:
            kind, retry_at = self._reported_backoff
            if now >= retry_at:
                self._reported_backoff = None
                log.bind(rate_limit_kind=kind, retry_at=retry_at).info(
                    "github_rest_resumed"
                )

        counts = self._counts.setdefault(operation, Counter())
        counts["request_count"] += 1
        counts["conditional_count"] += conditional_sent
        counts["not_modified_count"] += status == 304 and failure is None
        counts["failed_count"] += failure is not None
        if now - self._window_start >= _SUMMARY_SECONDS:
            for operation_name, totals in sorted(self._counts.items()):
                event_logger(
                    component=self.component,
                    repository=self.repository,
                    operation=operation_name,
                    window_started_at=self._window_start,
                    window_ended_at=now,
                    **quota,
                    **totals,
                ).info("github_rest_request_summary")
            self._counts.clear()
            self._window_start = now

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


def _rate_limit_kind(
    status: int | None, headers: dict[str, str], body: str, stderr: str
) -> str | None:
    if status not in (403, 429):
        return None
    if _integer(headers.get("x-ratelimit-remaining")) == 0:
        return "primary"
    if (
        status == 429
        or "retry-after" in headers
        or "rate limit" in body.lower()
        or "rate limit" in stderr.lower()
    ):
        return "secondary"
    return None


def _cli_failure(status: int | None, stderr: str) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status is not None:
        return "http_error" if status >= 400 else "unexpected_response"
    message = stderr.lower()
    if "gh auth login" in message:
        return "authentication_required"
    if any(
        part in message for part in ("dial tcp", "error connecting", "tls handshake")
    ):
        return "transport_error"
    return "gh_cli_error"


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
