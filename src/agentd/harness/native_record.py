"""Read final counters from a protected, version-pinned Codex native record.

This is a recovery proof, never a replacement quota source during execution.
The caller must own the worker process fence; the model must have no access to
this dedicated Codex home. Unknown/truncated formats remain unresolved.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agentd.domain.models import JsonValue, TokenUsage
from agentd.harness.app_server import PINNED_OPENAI_CODEX_VERSION

_MAX_RECORD_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class NativeTerminalUsage:
    total: TokenUsage
    record_sha256: str


def read_native_terminal_usage(
    thread: Mapping[str, JsonValue],
    thread_id: str,
    turn_id: str,
    *,
    codex_home: Path,
    runtime_version: str,
) -> NativeTerminalUsage:
    """Require exact terminal turn, native identity, and complete final counters."""
    if runtime_version != PINNED_OPENAI_CODEX_VERSION:
        raise ValueError("Native recovery format requires the pinned Codex runtime")
    raw_path = thread.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ValueError("Saved Codex thread has no native record path")
    home = codex_home.resolve(strict=True)
    path = Path(raw_path)
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise ValueError("Native record escapes protected Codex home") from error
    if (
        not relative.parts
        or relative.parts[0] not in {"sessions", "archived_sessions"}
        or any(part in {".", ".."} for part in relative.parts)
        or path.suffix != ".jsonl"
    ):
        raise ValueError("Native record is outside the saved session directories")
    parent = home
    for part in relative.parts[:-1]:
        parent /= part
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("Native record parent is not a regular directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RECORD_BYTES:
            raise ValueError("Native record is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        after = os.fstat(fd)
        if (
            len(raw) > _MAX_RECORD_BYTES
            or not raw.endswith(b"\n")
            or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("Native record is incomplete or changed during recovery")
    finally:
        os.close(fd)
    return _parse_native_terminal_usage(
        raw,
        thread=thread,
        thread_id=thread_id,
        turn_id=turn_id,
        runtime_version=runtime_version,
    )


def _parse_native_terminal_usage(
    raw: bytes,
    *,
    thread: Mapping[str, JsonValue],
    thread_id: str,
    turn_id: str,
    runtime_version: str,
) -> NativeTerminalUsage:
    active: str | None = None
    seen: set[str] = set()
    latest_complete: str | None = None
    total: TokenUsage | None = None
    current_usage: TokenUsage | None = None
    completed_usage: TokenUsage | None = None
    identity_seen = False
    for index, line in enumerate(raw.splitlines()):
        if not line or len(line) > _MAX_LINE_BYTES:
            raise ValueError("Native record contains an invalid or oversized row")
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise ValueError("Native record row format is unknown")
        payload = row["payload"]
        if row.get("type") == "session_meta":
            if (
                index != 0
                or identity_seen
                or payload.get("id") != thread_id
                or payload.get("cwd") != thread.get("cwd")
                or payload.get("cli_version") != runtime_version
            ):
                raise ValueError("Native record session identity mismatch")
            identity_seen = True
            continue
        if not identity_seen:
            raise ValueError("Native record lacks its original session identity")
        if row.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "task_started":
            started = payload.get("turn_id")
            if (
                active is not None
                or not isinstance(started, str)
                or not started
                or started in seen
            ):
                raise ValueError("Native record has ambiguous provider turn ownership")
            active = started
            seen.add(started)
            current_usage = None
        elif kind == "token_count":
            info = payload.get("info")
            if info is None:
                continue
            if not isinstance(info, dict):
                raise ValueError("Native token counters have an unknown format")
            counters = info.get("total_token_usage")
            parsed = _native_tokens(counters)
            if total is not None and not parsed.dominates(total):
                raise ValueError("Native cumulative usage moved backwards")
            if active is None:
                # A count emitted outside a terminal turn cannot establish a
                # new final charge for that turn.
                if total is None or parsed != total:
                    raise ValueError("Native usage appeared outside its provider turn")
            else:
                current_usage = parsed
            total = parsed
        elif kind == "task_complete":
            if active is None or payload.get("turn_id") != active:
                raise ValueError("Native terminal marker belongs to another turn")
            latest_complete = active
            if active == turn_id:
                completed_usage = current_usage
            active = None
            current_usage = None
        elif kind in {"turn_aborted", "task_aborted"}:
            # Interrupted/in-flight calls may have unreported provider usage.
            raise ValueError("Native interrupted turn has unresolved final usage")
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
        raise ValueError("Saved thread has no exact terminal turn")
    terminal = turns[-1]
    if (
        not identity_seen
        or active is not None
        or latest_complete != turn_id
        or completed_usage is None
        or terminal.get("id") != turn_id
        or terminal.get("status") != "completed"
    ):
        raise ValueError("Native record does not prove the latest completed turn usage")
    return NativeTerminalUsage(completed_usage, hashlib.sha256(raw).hexdigest())


def _native_tokens(raw: object) -> TokenUsage:
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    if not isinstance(raw, dict) or any(
        not isinstance(raw.get(key), int) or isinstance(raw.get(key), bool)
        for key in keys
    ):
        raise ValueError("Native token counters must be complete integers")
    usage = TokenUsage(**{key: raw[key] for key in keys[:-1]})
    if raw["total_tokens"] != usage.total_tokens:
        raise ValueError("Native total token counters disagree")
    return usage
