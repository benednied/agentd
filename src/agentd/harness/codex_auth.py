"""Host-owned refresh for the dedicated, file-backed Codex CLI session."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from http.client import HTTPException
from math import isfinite
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Native Codex CLI grant parameters for the pinned runtime.
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_LIMIT = 1_048_576
_TERMINAL = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
        "refresh_rejected",
        "token_expired",
    }
)
_REAUTH_FIELD = "agentd_reauthorization"
_RETRY_FIELD = "agentd_refresh_retry_after"
_REFRESH_BACKOFF_SECONDS = 15
REAUTHORIZATION_REQUIRED = "reauthorization_required"
REAUTHORIZATION_ACTION = (
    "Run agentd --codex-home PATH codex-reauthorize on the credential owner's "
    "host, then run codex-status against the same Codex home."
)


class CodexReauthorizationRequired(RuntimeError):
    """A fixed, safe error suitable for status output and daemon logs."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in _TERMINAL else "session_unavailable"
        super().__init__(REAUTHORIZATION_ACTION)


class CodexRefreshUnavailable(RuntimeError):
    """A transient or local failure with no upstream response details."""


@dataclass(frozen=True, repr=False, slots=True)
class CodexAccess:
    token: str
    account_id: str
    expires_at: float
    last_refresh: str | None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def _exchange(refresh_token: str) -> dict[str, object]:
    request = Request(
        _TOKEN_URL,
        data=json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": _CLIENT_ID,
                "refresh_token": refresh_token,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=12) as response:
            result = json.loads(response.read(_LIMIT + 1))
            if isinstance(result, dict):
                return result
    except HTTPError as error:
        with error:
            try:
                body = json.loads(error.read(_LIMIT + 1))
            except (HTTPException, OSError, ValueError):
                body = None
        detail = body.get("error") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else detail
        if (
            error.code in {400, 401, 403}
            and isinstance(code, str)
            and code in _TERMINAL
        ):
            raise CodexReauthorizationRequired(code) from None
        if error.code == 401 and code != "invalid_client":
            raise CodexReauthorizationRequired("refresh_rejected") from None
    except (HTTPException, OSError, ValueError):
        pass
    raise CodexRefreshUnavailable("Codex credential refresh unavailable")


def _access(tokens: dict[str, object], last_refresh: object) -> CodexAccess:
    token, account = tokens.get("access_token"), tokens.get("account_id")
    if isinstance(token, str) and isinstance(account, str) and account:
        try:
            _, payload, _ = token.split(".")
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            expiry = claims.get("exp") if isinstance(claims, dict) else None
            if type(expiry) in (int, float) and isfinite(expiry):
                return CodexAccess(
                    token,
                    account,
                    expiry,
                    last_refresh if isinstance(last_refresh, str) else None,
                )
        except (ValueError, OverflowError):
            pass
    raise CodexReauthorizationRequired("session_unavailable")


def _load(path: Path) -> dict[str, object]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > _LIMIT
            ):
                raise CodexReauthorizationRequired("session_unavailable")
            record = json.loads(stream.read(_LIMIT + 1))
    except (OSError, ValueError):
        raise CodexReauthorizationRequired("session_unavailable") from None
    if not isinstance(record, dict):
        raise CodexReauthorizationRequired("session_unavailable")
    return record


def _tokens(record: dict[str, object]) -> dict[str, object]:
    tokens = record.get("tokens")
    if (
        record.get("auth_mode") not in ("chatgpt", None)
        or not isinstance(tokens, dict)
        or not isinstance(tokens.get("refresh_token"), str)
        or not tokens["refresh_token"]
    ):
        raise CodexReauthorizationRequired("session_unavailable")
    return tokens


class CodexSession:
    """Serialize exchanges across local processes sharing one protected home."""

    def __init__(
        self,
        home: Path,
        *,
        exchange: Callable[[str], dict[str, object]] = _exchange,
    ) -> None:
        self.home = home.resolve()
        self._auth = self.home / "auth.json"
        self._exchange = exchange

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            info = self.home.stat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise CodexReauthorizationRequired("session_unavailable")
            fd = os.open(
                self.home / ".agentd-auth.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except FileNotFoundError:
            raise CodexReauthorizationRequired("session_unavailable") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def access(self, *, rejected: CodexAccess | None = None) -> CodexAccess:
        with self._locked():
            record = _load(self._auth)
            reason = record.get(_REAUTH_FIELD)
            if isinstance(reason, str) and reason in _TERMINAL:
                raise CodexReauthorizationRequired(reason)
            tokens = _tokens(record)
            current = _access(tokens, record.get("last_refresh"))
            if rejected is not None and current.account_id != rejected.account_id:
                raise CodexReauthorizationRequired("session_unavailable")
            now = time.time()
            if rejected is None and current.expires_at > now + 300:
                return current
            if (
                rejected is not None
                and current != rejected
                and current.expires_at > now
            ):
                return current
            retry_after = record.get(_RETRY_FIELD)
            if type(retry_after) in (int, float) and (
                0 < retry_after - now <= _REFRESH_BACKOFF_SECONDS
            ):
                raise CodexRefreshUnavailable("Codex credential refresh unavailable")
            try:
                updated = self._rotate(record, tokens)
            except CodexReauthorizationRequired as error:
                record[_REAUTH_FIELD] = error.reason
                self._save(record)
                raise
            except CodexRefreshUnavailable:
                record[_RETRY_FIELD] = time.time() + _REFRESH_BACKOFF_SECONDS
                self._save(record)
                raise
            record.pop(_RETRY_FIELD, None)
            self._save(record)
            return updated

    def _rotate(
        self, record: dict[str, object], tokens: dict[str, object]
    ) -> CodexAccess:
        rotated = self._exchange(str(tokens["refresh_token"]))
        replacement = rotated.get("access_token")
        if not isinstance(replacement, str) or not replacement:
            raise CodexRefreshUnavailable("Codex returned incomplete credentials")
        candidate = {**tokens, "access_token": replacement}
        for key in ("refresh_token", "id_token"):
            value = rotated.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise CodexRefreshUnavailable("Codex returned invalid credentials")
                candidate[key] = value
        last_refresh = datetime.now(UTC).isoformat()
        try:
            updated = _access(candidate, last_refresh)
        except CodexReauthorizationRequired:
            raise CodexRefreshUnavailable(
                "Codex returned invalid credentials"
            ) from None
        record.update(tokens=candidate, last_refresh=last_refresh)
        return updated

    def install(self, source: Path) -> None:
        record = _load(source)
        _access(_tokens(record), record.get("last_refresh"))
        record.pop(_REAUTH_FIELD, None)
        record.pop(_RETRY_FIELD, None)
        with self._locked():
            self._save(record)

    def _save(self, record: dict[str, object]) -> None:
        fd, name = tempfile.mkstemp(prefix=".agentd-auth-", dir=self.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._auth)
            directory = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
