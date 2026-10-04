import hashlib
import json

import pytest

from agentd.harness.app_server import PINNED_OPENAI_CODEX_VERSION
from agentd.harness.native_record import read_native_terminal_usage


def _tokens(input_tokens=12, output_tokens=8):
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": 3,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 2,
        "total_tokens": input_tokens + output_tokens,
    }


def _row(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


@pytest.fixture(params=["0.144.4", PINNED_OPENAI_CODEX_VERSION])
def native_record(tmp_path, request):
    home = tmp_path / "codex-home"
    root = home / "sessions" / "2026" / "10" / "03"
    root.mkdir(parents=True)
    path = root / "rollout-thread.jsonl"
    thread = {
        "id": "thread",
        "cwd": "/workspace",
        "path": str(path),
        "turns": [{"id": "turn", "status": "completed"}],
    }
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "thread",
                "cwd": "/workspace",
                "cli_version": request.param,
            },
        },
        _row("task_started", turn_id="prior"),
        _row("token_count", info={"total_token_usage": _tokens(8, 4)}),
        _row("task_complete", turn_id="prior"),
        _row("task_started", turn_id="turn"),
        _row("token_count", info={"total_token_usage": _tokens()}),
        _row("task_complete", turn_id="turn"),
    ]

    def write():
        raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        path.write_bytes(raw)
        return raw

    write()
    return home, path, thread, rows, write


def _read(record):
    home, _path, thread, rows, _write = record
    return read_native_terminal_usage(
        thread,
        "thread",
        "turn",
        codex_home=home,
        runtime_version=(
            rows[0]["payload"]["cli_version"]
            if rows[0]["payload"]["cli_version"] != "unknown"
            else PINNED_OPENAI_CODEX_VERSION
        ),
    )


def test_native_usage_requires_exact_completed_turn_and_hashes_original_record(
    native_record,
):
    proof = _read(native_record)
    assert proof.total.total_tokens == 20
    assert proof.total.cached_input_tokens == 3
    assert (
        proof.record_sha256 == hashlib.sha256(native_record[1].read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-thread",
        "wrong-cwd",
        "wrong-version",
        "wrong-turn",
        "wrong-read-turn",
        "unfinished",
        "missing-final-usage",
        "backwards-usage",
        "invalid-total",
        "boolean-counter",
        "newer-turn",
        "duplicate-start",
        "interrupted",
    ],
)
def test_native_recovery_rejects_unverified_identity_or_usage(native_record, fault):
    _home, _path, thread, rows, write = native_record
    if fault == "wrong-thread":
        rows[0]["payload"]["id"] = "other"
    elif fault == "wrong-cwd":
        rows[0]["payload"]["cwd"] = "/other"
    elif fault == "wrong-version":
        rows[0]["payload"]["cli_version"] = "unknown"
    elif fault == "wrong-turn":
        rows[-1]["payload"]["turn_id"] = "other"
    elif fault == "wrong-read-turn":
        thread["turns"][-1]["id"] = "other"
    elif fault == "unfinished":
        rows.pop()
    elif fault == "missing-final-usage":
        rows.pop(-2)
    elif fault == "backwards-usage":
        rows[-2]["payload"]["info"]["total_token_usage"] = _tokens(6, 3)
    elif fault == "invalid-total":
        rows[-2]["payload"]["info"]["total_token_usage"]["total_tokens"] = 999
    elif fault == "boolean-counter":
        rows[-2]["payload"]["info"]["total_token_usage"]["input_tokens"] = True
    elif fault == "newer-turn":
        rows.append(_row("task_started", turn_id="newer"))
    elif fault == "duplicate-start":
        rows.insert(-1, _row("task_started", turn_id="turn"))
    elif fault == "interrupted":
        rows[-1] = _row("turn_aborted", turn_id="turn")
        thread["turns"][-1]["status"] = "interrupted"
    write()
    with pytest.raises(ValueError):
        _read(native_record)


def test_native_recovery_rejects_truncated_record(native_record):
    path = native_record[1]
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(ValueError, match="incomplete"):
        _read(native_record)


def test_native_recovery_rejects_symlink_and_external_paths(native_record, tmp_path):
    _home, path, thread, _rows, _write = native_record
    outside = tmp_path / "external.jsonl"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        _read(native_record)
    thread["path"] = str(outside)
    with pytest.raises(ValueError, match="escapes"):
        _read(native_record)


def test_native_recovery_rejects_symlink_parent(native_record):
    home, path, thread, _rows, _write = native_record
    (home / "linked").symlink_to(path.parent, target_is_directory=True)
    (home / "sessions" / "alias").symlink_to(path.parent, target_is_directory=True)
    thread["path"] = str(home / "sessions" / "alias" / path.name)
    with pytest.raises(ValueError, match="parent"):
        _read(native_record)
