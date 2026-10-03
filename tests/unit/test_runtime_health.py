import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from agentd.domain.enums import QuotaUnit
from agentd.domain.models import QuotaPool, WorkerHeartbeat
from agentd.runtime.health import (
    RuntimeHealthStore,
    RuntimeHeartbeat,
    runtime_liveness,
    worker_heartbeat_ready,
)
from agentd.state.sqlite import SQLiteStateStore

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def test_pulses_survive_restart_and_leave_execution_schema_untouched(tmp_path):
    path = tmp_path / "state.sqlite"
    with SQLiteStateStore(path) as state:
        pool = QuotaPool("codex", "provider", 7, unit=QuotaUnit.TOKENS)
        state.save_quota_pool(pool)
        version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        with RuntimeHealthStore(path) as health:
            assert health.latest("controller") is None
            pulse = health.pulse("controller", state="waiting_quota", at=NOW)
            health.pulse("publisher", state="waiting_review", at=NOW)
            assert health.latest("controller") == pulse
        assert state.get_quota_pool("codex") == pool
    with RuntimeHealthStore(path) as health:
        assert health.latest("controller") == pulse
        assert health.latest("publisher").state == "waiting_review"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version


def test_stale_pulse_cannot_replace_newer_role_evidence(tmp_path):
    path = tmp_path / "state.sqlite"
    with RuntimeHealthStore(path) as first, RuntimeHealthStore(path) as second:
        fresh = first.pulse("controller", at=NOW)
        stale = second.pulse(
            "controller", state="waiting_quota", at=NOW - timedelta(seconds=1)
        )
        assert stale == fresh
        assert first.latest("controller") == fresh


@pytest.mark.parametrize("state", ["waiting_quota", "waiting_dependencies", "idle"])
def test_expected_wait_remains_live_with_fresh_locked_role(state):
    pulse = RuntimeHeartbeat("controller", state, NOW)
    health = runtime_liveness(pulse, owner_live=True, at=NOW + timedelta(seconds=180))
    assert health["live"]
    assert health["expected_wait"]
    assert health["reason"] == "heartbeat_fresh"
    assert health["state"] == state
    # A formerly healthy wait is not an excuse for an abandoned poller.
    stale = runtime_liveness(pulse, owner_live=True, at=NOW + timedelta(seconds=181))
    assert not stale["live"]
    assert not stale["expected_wait"]
    assert stale["reason"] == "heartbeat_stale"


def test_fresh_ownership_blocked_pulse_is_live_but_not_an_expected_wait():
    pulse = RuntimeHeartbeat("controller", "ownership_blocked", NOW)
    health = runtime_liveness(pulse, owner_live=True, at=NOW)
    assert health["live"]
    assert not health["expected_wait"]
    assert health["state"] == "ownership_blocked"


def test_lock_alone_or_retained_pulse_alone_cannot_prove_liveness():
    pulse = RuntimeHeartbeat("controller", "polling", NOW)
    missing = runtime_liveness(None, owner_live=True, at=NOW)
    assert not missing["live"] and missing["reason"] == "heartbeat_missing"
    exited = runtime_liveness(pulse, owner_live=False, at=NOW)
    assert not exited["live"] and exited["reason"] == "owner_not_live"
    future = runtime_liveness(pulse, owner_live=True, at=NOW - timedelta(seconds=1))
    assert not future["live"]
    assert future["reason"] == "heartbeat_future_or_invalid"


def test_pulse_rejects_arbitrary_text_and_non_utc_observations():
    with pytest.raises(ValueError, match="labels"):
        RuntimeHeartbeat("controller", "secret: some arbitrary output", NOW)
    with pytest.raises(ValueError, match="UTC"):
        RuntimeHeartbeat("controller", "polling", datetime(2026, 10, 3, 12))
    with pytest.raises(ValueError, match="UTC"):
        RuntimeHeartbeat(
            "controller",
            "polling",
            NOW.astimezone(timezone(timedelta(hours=2))),
        )
    with pytest.raises(ValueError, match="finite and positive"):
        runtime_liveness(None, owner_live=True, stale_after_seconds=float("nan"))


def test_worker_readiness_requires_fresh_authenticated_configured_identity():
    heartbeat = WorkerHeartbeat("epoch-v1", frozenset({"remote-coding"}), 1, NOW)
    assert worker_heartbeat_ready(
        heartbeat, expected_epoch="epoch-v1", at=NOW + timedelta(seconds=45)
    )
    assert not worker_heartbeat_ready(
        heartbeat, expected_epoch="epoch-v1", at=NOW + timedelta(seconds=46)
    )
    assert not worker_heartbeat_ready(heartbeat, expected_epoch="epoch-v2", at=NOW)
    assert not worker_heartbeat_ready(heartbeat, at=NOW - timedelta(seconds=1))
    assert not worker_heartbeat_ready(None, at=NOW)
    unsupported = WorkerHeartbeat("epoch-v1", frozenset({"fake"}), 0, NOW)
    assert not worker_heartbeat_ready(unsupported, at=NOW)


def test_worker_readiness_ignores_active_run_count_as_ownership_proof():
    active = WorkerHeartbeat("epoch-v1", frozenset({"remote-coding"}), 1, NOW)
    idle = WorkerHeartbeat("epoch-v1", frozenset({"remote-coding"}), 0, NOW)
    assert worker_heartbeat_ready(active, at=NOW)
    assert worker_heartbeat_ready(idle, at=NOW)
