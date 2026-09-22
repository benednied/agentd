from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from agentd.domain.enums import QoSClass, QuotaMode
from agentd.domain.models import (
    ProviderQuotaSnapshot,
    QuotaPool,
    QuotaResetEvent,
    utc_now,
)
from agentd.domain.transitions import initial_transition
from agentd.runtime.accounts import unattended_provider_wait_reason
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.state.sqlite import SQLiteStateStore


def _job(make_job, **changes):
    job = make_job(qos=QoSClass.SCAVENGER, **changes)
    return replace(job, quota_budget=replace(job.quota_budget, maximum=32))


def _snapshot(**changes):
    return ProviderQuotaSnapshot(
        pool_id="default", bucket_id="account", primary_used_percent=10, **changes
    )


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (None, "quota_unknown"),
        (replace(_snapshot(), primary_used_percent=None), "quota_unknown"),
        (replace(_snapshot(), confidence=0), "quota_unknown"),
        (_snapshot(observed_at=utc_now() - timedelta(minutes=6)), "quota_stale"),
        (_snapshot(observed_at=utc_now() + timedelta(minutes=1)), "quota_stale"),
        (replace(_snapshot(), primary_used_percent=75), "quota_provider_pressure"),
        (replace(_snapshot(), reached=True), "quota_provider_pressure"),
        (_snapshot(), None),
    ],
)
def test_unattended_provider_gate(snapshot, reason):
    assert unattended_provider_wait_reason(snapshot) == reason


def test_unknown_quota_cannot_reserve_and_reason_survives_restart(make_job, tmp_path):
    path = tmp_path / "state.db"
    job = _job(make_job)
    with SQLiteStateStore(path) as store:
        store.create_job(job, initial_transition(job))
        store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
        with pytest.raises(QuotaAdmissionError) as caught:
            QuotaManager(store).reserve(job)
        assert caught.value.reason == "quota_unknown"
        assert store.get_quota_pool("default").reserved == 0
    with SQLiteStateStore(path) as store:
        assert QuotaManager(store).wait_reason(store.get_job(job.id)) == "quota_unknown"
        store.append_provider_quota_snapshot(_snapshot())
        assert QuotaManager(store).reserve(job).amount == 16


def test_shared_account_cannot_over_admit_and_restart_retains_reservation(
    make_job, tmp_path
):
    path = tmp_path / "state.db"
    jobs = [_job(make_job), _job(make_job)]
    with SQLiteStateStore(path) as store:
        for job in jobs:
            store.create_job(job, initial_transition(job))
        store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=20))
        store.append_provider_quota_snapshot(_snapshot())

    def reserve(job):
        with SQLiteStateStore(path) as store:
            try:
                return QuotaManager(store).reserve(job)
            except QuotaAdmissionError as error:
                return error.reason

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, jobs))
    assert results.count("quota_insufficient") == 1
    with SQLiteStateStore(path) as store:
        assert store.get_quota_pool("default").reserved == 16
        winner = next(job for job in jobs if store.find_active_reservation(job.id))
        previous = store.find_active_reservation(winner.id)
        # Lost controller acknowledgement is reconciled to the existing reservation.
        assert QuotaManager(store).reserve(winner) == previous
        QuotaManager(store).register_reset_event(
            QuotaResetEvent(
                pool_id="default",
                mode=QuotaMode.RESET_CONFIRMED,
                confidence=1,
                new_remaining=100,
            )
        )
        assert store.get_quota_pool("default").reserved == 16
        assert store.find_active_reservation(winner.id) == previous


def test_announced_reset_does_not_infer_new_provider_balance():
    now = utc_now()
    snapshot = _snapshot(
        observed_at=now - timedelta(seconds=30),
        primary_reset_at=now - timedelta(seconds=10),
    )
    assert unattended_provider_wait_reason(snapshot, at=now) == "quota_stale"
    assert (
        unattended_provider_wait_reason(replace(snapshot, observed_at=now), at=now)
        is None
    )


def test_unattended_requires_bounded_total_budget(make_job):
    job = make_job(qos=QoSClass.SCAVENGER)
    with SQLiteStateStore() as store:
        assert QuotaManager(store).wait_reason(job) == "quota_unbounded"


def test_existing_reservation_reconciles_but_unknown_quota_blocks_top_up(make_job):
    job = _job(make_job)
    with SQLiteStateStore() as store:
        store.create_job(job, initial_transition(job))
        store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
        store.append_provider_quota_snapshot(_snapshot())
        manager = QuotaManager(store)
        reservation = manager.reserve(job)
        store.append_provider_quota_snapshot(
            replace(_snapshot(), primary_used_percent=None)
        )
        assert manager.reserve(job) == reservation
        with pytest.raises(QuotaAdmissionError) as caught:
            manager.top_up(reservation.id, 1)
        assert caught.value.reason == "quota_unknown"
        assert store.get_quota_pool("default").reserved == 16
