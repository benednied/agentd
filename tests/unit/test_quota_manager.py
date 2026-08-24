from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import QoSClass, QuotaMode, ReservationState
from agentd.domain.models import Job, QuotaPool, QuotaResetEvent
from agentd.domain.transitions import initial_transition
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.state.sqlite import SQLiteStateStore


def _store_with_job(job: Job, pool: QuotaPool) -> SQLiteStateStore:
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(pool)
    return store


def test_reserves_full_path_to_accepted_artifact(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store = _store_with_job(
        job,
        QuotaPool(id="default", provider="fake", remaining=100),
    )

    reservation = QuotaManager(store).reserve(job)

    assert reservation.amount == 16
    assert store.get_quota_pool("default").reserved == 16


def test_noninteractive_job_cannot_consume_interactive_reserve(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store = _store_with_job(
        job,
        QuotaPool(
            id="default",
            provider="fake",
            remaining=20,
            minimum_interactive_reserve=5,
        ),
    )

    with pytest.raises(QuotaAdmissionError, match="only 15"):
        QuotaManager(store).reserve(job)

    assert store.get_quota_pool("default").reserved == 0


def test_interactive_job_may_use_reserved_headroom(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(qos=QoSClass.INTERACTIVE)
    store = _store_with_job(
        job,
        QuotaPool(
            id="default",
            provider="fake",
            remaining=16,
            minimum_interactive_reserve=5,
        ),
    )

    assert QuotaManager(store).reserve(job).amount == 16


def test_release_is_idempotent_and_accounts_consumption(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store = _store_with_job(
        job,
        QuotaPool(id="default", provider="fake", remaining=100),
    )
    manager = QuotaManager(store)
    reservation = manager.reserve(job)

    released = manager.release(reservation.id, consumed=7)
    released_again = manager.release(reservation.id, consumed=9)

    assert released.state == ReservationState.RELEASED
    assert released_again == released
    assert store.get_quota_pool("default").remaining == 93
    assert store.get_quota_pool("default").reserved == 0


def test_emergency_conserve_rejects_background_work(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store = _store_with_job(
        job,
        QuotaPool(
            id="default",
            provider="fake",
            remaining=100,
            mode=QuotaMode.EMERGENCY_CONSERVE,
        ),
    )

    with pytest.raises(QuotaAdmissionError, match="conserving"):
        QuotaManager(store).reserve(job)


def test_external_oracle_can_announce_and_confirm_reset(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    pool = QuotaPool(id="default", provider="fake", remaining=2)
    store = _store_with_job(job, pool)
    manager = QuotaManager(store)

    announced = manager.register_reset_event(
        QuotaResetEvent(
            pool_id="default",
            mode=QuotaMode.PRE_RESET_BURN,
            confidence=0.8,
        )
    )
    confirmed = manager.register_reset_event(
        QuotaResetEvent(
            pool_id="default",
            mode=QuotaMode.RESET_CONFIRMED,
            confidence=1,
            new_remaining=100,
        )
    )

    assert announced.mode == QuotaMode.PRE_RESET_BURN
    assert confirmed.remaining == 100
    assert confirmed.reset_confidence == 1


def test_reserve_is_idempotent(make_job: Callable[..., Job]) -> None:
    job = make_job()
    store = _store_with_job(
        job,
        QuotaPool(id="default", provider="fake", remaining=100),
    )
    manager = QuotaManager(store)

    first = manager.reserve(job)
    second = manager.reserve(replace(job, priority=99))

    assert second == first
    assert store.get_quota_pool("default").reserved == first.amount
