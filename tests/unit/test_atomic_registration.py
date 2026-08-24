from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import QuotaMode
from agentd.domain.models import (
    Job,
    QuotaPool,
    QuotaResetEvent,
    ResourceVector,
    WorkerNode,
)
from agentd.domain.transitions import initial_transition
from agentd.runtime.quota import QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.service import ControlPlane
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore


def _runtime(job: Job) -> tuple[SQLiteStateStore, WorkerNode]:
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    node = WorkerNode(
        id="node",
        labels={"zone": "old"},
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset({"fake"}),
    )
    store.save_node(node)
    return store, node


def test_registration_preserves_live_counters_and_releasability(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    store, node = _runtime(job)
    reservation = QuotaManager(store).reserve(job)
    allocation = ResourceManager(store).allocate(job, node)
    plane = ControlPlane(store)

    registered_node = plane.register_node(
        replace(
            node,
            labels={"zone": "new"},
            capacity=ResourceVector(cpu=16, ram_gb=32),
            allocated=ResourceVector(0, 0),
        )
    )
    registered_pool = plane.register_quota_pool(
        QuotaPool(
            id="default",
            provider="updated",
            remaining=999,
            reserved=0,
            minimum_interactive_reserve=7,
        )
    )

    assert registered_node.allocated == job.resources
    assert registered_node.labels == {"zone": "new"}
    assert registered_pool.remaining == 100
    assert registered_pool.reserved == reservation.amount
    assert registered_pool.provider == "updated"
    ResourceManager(store).release(allocation.id)
    QuotaManager(store).release(reservation.id, consumed=3)
    assert store.get_node(node.id).allocated == ResourceVector(0, 0)
    assert store.get_quota_pool("default").remaining == 97
    assert store.get_quota_pool("default").reserved == 0


def test_stale_quota_mutation_cannot_erase_live_reservation(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store, _node = _runtime(job)
    stale = store.get_quota_pool("default")
    reservation = QuotaManager(store).reserve(job)
    stale_reset = replace(
        stale,
        mode=QuotaMode.RESET_CONFIRMED,
        remaining=200,
        reset_confidence=1,
    )

    with pytest.raises(ConcurrentStateError, match="changed concurrently"):
        store.update_quota_pool(stale, stale_reset)

    current = store.get_quota_pool("default")
    assert current.remaining == 100
    assert current.reserved == reservation.amount
    QuotaManager(store).release(reservation.id)


class ReserveDuringResetStore(SQLiteStateStore):
    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.injected = False

    def update_quota_pool(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
    ) -> None:
        if not self.injected:
            self.injected = True
            QuotaManager(self).reserve(self.job)
        super().update_quota_pool(expected_pool, updated_pool)


def test_reset_event_retries_concurrent_reservation_without_lost_counter(
    make_job: Callable[..., Job],
) -> None:
    job = make_job()
    store = ReserveDuringResetStore(job)
    store.create_job(job, initial_transition(job))
    store.save_quota_pool(QuotaPool(id="default", provider="fake", remaining=100))
    manager = QuotaManager(store)

    reset = manager.register_reset_event(
        QuotaResetEvent(
            pool_id="default",
            mode=QuotaMode.RESET_CONFIRMED,
            confidence=1,
            new_remaining=200,
        )
    )

    reservation = store.find_active_reservation(job.id)
    assert reservation is not None
    assert store.injected
    assert reset.remaining == 200
    assert reset.reserved == reservation.amount
    assert store.get_quota_pool("default") == reset
