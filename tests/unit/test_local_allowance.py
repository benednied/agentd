from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import QuotaMode, QuotaUnit
from agentd.domain.models import ProviderQuotaSnapshot, QuotaPool
from agentd.runtime.accounts import unattended_provider_wait_reason
from agentd.runtime.allowance import CheckpointBudgetPolicy, LocalAllowancePolicy
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _policy(**changes):
    return replace(LocalAllowancePolicy("codex", "daily-v1", 500_000), **changes)


def _store(path=":memory:"):
    store = SQLiteStateStore(path)
    store.save_quota_pool(
        QuotaPool(
            "codex",
            "openai",
            12,
            reserved=4,
            debt=3,
            unit=QuotaUnit.TOKENS,
            minimum_interactive_reserve=100,
            reset_at=NOW + timedelta(hours=5),
            reset_confidence=0.75,
            mode=QuotaMode.EMERGENCY_CONSERVE,
        )
    )
    return store


def test_window_replay_and_restart_do_not_refill_spent_allowance(tmp_path):
    path = tmp_path / "state.sqlite"
    store = _store(path)
    original = store.get_quota_pool("codex")
    updated = _policy().reconcile(store, at=NOW)
    assert updated.remaining == 500_000
    assert replace(updated, remaining=12, updated_at=original.updated_at) == original
    spent = replace(updated, remaining=90)
    store.update_quota_pool(updated, spent)
    assert _policy().reconcile(store, at=NOW + timedelta(hours=1)) == spent
    store.close()
    with SQLiteStateStore(path) as reopened:
        assert _policy().reconcile(reopened, at=NOW + timedelta(hours=2)) == spent
        assert len(reopened.list_reset_events("codex")) == 1


def test_missed_windows_do_not_accumulate_and_old_window_cannot_refill():
    with _store() as store:
        _policy().reconcile(store, at=NOW)
        future = _policy().reconcile(store, at=NOW + timedelta(days=4))
        assert future.remaining == 500_000
        spent = replace(future, remaining=7)
        store.update_quota_pool(future, spent)
        # This previously unseen earlier window is rejected after clock rollback.
        assert _policy().reconcile(store, at=NOW + timedelta(days=2)) == spent
        assert len(store.list_reset_events("codex")) == 2


@pytest.mark.parametrize(
    "changes", [{"tokens_per_window": 600_000}, {"window_seconds": 3_600}]
)
def test_existing_policy_cannot_silently_change_across_windows(changes):
    with _store() as store:
        _policy().reconcile(store, at=NOW)
        with pytest.raises(ConcurrentStateError):
            _policy(**changes).reconcile(store, at=NOW + timedelta(days=1))
        assert store.get_quota_pool("codex").remaining == 500_000
        assert len(store.list_reset_events("codex")) == 1


def test_concurrent_grants_commit_only_one_window(tmp_path):
    path = tmp_path / "state.sqlite"
    store = _store(path)
    peers = [SQLiteStateStore(path) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            granted = list(
                executor.map(lambda peer: _policy().reconcile(peer, at=NOW), peers)
            )
        assert all(pool.remaining == 500_000 for pool in granted)
        assert len(store.list_reset_events("codex")) == 1
    finally:
        for peer in peers:
            peer.close()
        store.close()


def test_local_grant_does_not_fabricate_provider_capacity():
    with _store() as store:
        _policy().reconcile(store, at=NOW)
        assert store.latest_provider_quota_snapshot("codex") is None
        assert unattended_provider_wait_reason(None, at=NOW) == "quota_unknown"
        pressure = ProviderQuotaSnapshot(
            pool_id="codex",
            bucket_id="codex",
            primary_used_percent=99,
            observed_at=NOW,
        )
        assert (
            unattended_provider_wait_reason(pressure, at=NOW)
            == "quota_provider_pressure"
        )


def test_allowance_before_anchor_has_no_grant_and_config_is_strict():
    policy = _policy(anchor=NOW + timedelta(days=1))
    assert policy.event_at(NOW) is None
    with pytest.raises(ValueError, match="UTC"):
        _policy(anchor=datetime(2026, 10, 3))
    with pytest.raises(ValueError, match="positive integer"):
        _policy(window_seconds=True)
    with pytest.raises(ValueError, match="Unknown"):
        LocalAllowancePolicy.from_config(
            {"policy_id": "daily", "tokens_per_window": 100, "provider_tokens": 100},
            pool_id="codex",
        )


def test_checkpoint_budget_growth_is_bounded_and_does_not_reset_usage():
    policy = CheckpointBudgetPolicy(maximum_tokens=300, increment_tokens=100)
    assert policy.replanned_maximum(100, 89) is None
    assert policy.replanned_maximum(100, 90) == 200
    assert policy.replanned_maximum(200, 190) == 300
    assert policy.replanned_maximum(300, 280) is None
    # An overshooting batch may use all new headroom; no futile retry is admitted.
    assert policy.replanned_maximum(100, 180) is None


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_checkpoint_budget_rejects_invalid_amounts(value):
    with pytest.raises(ValueError):
        CheckpointBudgetPolicy(maximum_tokens=value, increment_tokens=100)
    with pytest.raises(ValueError):
        CheckpointBudgetPolicy(maximum_tokens=300, increment_tokens=value)
