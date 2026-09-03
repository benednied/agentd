from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import QuotaMode
from agentd.domain.models import (
    ProviderQuotaSnapshot,
    QuotaPool,
    QuotaResetEvent,
)
from agentd.domain.transitions import initial_transition
from agentd.runtime.quota import QuotaManager
from agentd.runtime.reset import detect_provider_reset, reset_event_for_decision
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SCHEMA, SCHEMA_VERSION, SQLiteStateStore

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _pool_store(make_job, *, path: str = ":memory:") -> tuple[SQLiteStateStore, object]:
    store = SQLiteStateStore(path)
    job = make_job(id="reset-job")
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    return store, job


def _reset_event(
    *, event_id: str = "reset-event-1", remaining: float = 200
) -> QuotaResetEvent:
    return QuotaResetEvent(
        id=event_id,
        pool_id="default",
        mode=QuotaMode.RESET_CONFIRMED,
        confidence=1,
        new_remaining=remaining,
        source="configured-oracle",
    )


def test_quota_reset_event_id_round_trips_and_defaults() -> None:
    event = _reset_event()
    assert QuotaResetEvent.from_dict(event.to_dict()) == event
    assert QuotaResetEvent(pool_id="default", mode=QuotaMode.PRE_RESET_BURN).id


def test_exact_replay_after_consumption_returns_current_pool(
    make_job, tmp_path
) -> None:
    path = str(tmp_path / "reset-replay.sqlite")
    store, job = _pool_store(make_job, path=path)
    manager = QuotaManager(store)
    event = _reset_event()

    assert manager.register_reset_event(event).remaining == 200
    reservation = manager.reserve(job)
    manager.release(reservation.id, consumed=30)
    consumed_pool = store.get_quota_pool("default")
    assert consumed_pool.remaining == 170

    replayed = manager.register_reset_event(event)

    assert replayed == consumed_pool
    assert replayed.remaining == 170
    assert store.list_reset_events("default") == [event]
    store.close()

    reopened = SQLiteStateStore(path)
    assert QuotaManager(reopened).register_reset_event(event).remaining == 170
    assert reopened.list_reset_events("default") == [event]
    reopened.close()


def test_same_event_id_different_payload_conflicts_without_pool_change(
    make_job,
) -> None:
    store, _job = _pool_store(make_job)
    manager = QuotaManager(store)
    event = _reset_event()
    manager.register_reset_event(event)
    before = store.get_quota_pool("default")

    with pytest.raises(ConcurrentStateError, match="already differs"):
        manager.register_reset_event(_reset_event(remaining=201))

    assert store.get_quota_pool("default") == before
    assert store.list_reset_events() == [event]
    store.close()


def test_pool_and_event_are_rolled_back_together_on_pool_failure(make_job) -> None:
    store, _job = _pool_store(make_job)
    store._connection.execute(
        """
        CREATE TRIGGER reject_reset_pool_update
        BEFORE UPDATE OF payload ON quota_pools
        BEGIN
            SELECT RAISE(ABORT, 'injected pool failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected pool failure"):
        store.apply_reset_event(_reset_event())

    assert store.get_quota_pool("default").remaining == 100
    assert store.list_reset_events() == []
    store.close()


def test_v2_database_migrates_to_v3_reset_ledger(tmp_path) -> None:
    path = tmp_path / "v2.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA user_version = 2")

    store = SQLiteStateStore(path)
    assert store.schema_version == SCHEMA_VERSION
    table = store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'quota_reset_events'"
    ).fetchone()
    assert table is not None
    store.close()


def test_automatic_reset_event_uses_decision_identity() -> None:
    previous = ProviderQuotaSnapshot(
        id="before",
        provider="provider",
        pool_id="pool",
        bucket_id="bucket",
        primary_used_percent=90,
        primary_reset_at=NOW + timedelta(hours=1),
        observed_at=NOW,
    )
    current = ProviderQuotaSnapshot(
        id="after",
        provider="provider",
        pool_id="pool",
        bucket_id="bucket",
        primary_used_percent=10,
        primary_reset_at=NOW + timedelta(hours=2),
        observed_at=NOW + timedelta(minutes=1),
    )
    decision = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))
    event = reset_event_for_decision(decision, new_remaining=250)

    assert event is not None
    assert event.id == decision.event_id
