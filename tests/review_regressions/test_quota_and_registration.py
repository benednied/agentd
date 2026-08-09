from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from agentd.domain.enums import ReservationState
from agentd.domain.models import Job, QuotaPool, ResourceVector, WorkerNode
from agentd.runtime.quota import QuotaManager


def test_quota_release_charges_consumption_above_reserved_estimate(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_pool: QuotaPool,
) -> None:
    rig = make_regression_rig()
    job = make_regression_job()
    rig.plane.register_quota_pool(regression_pool)
    ready = rig.plane.submit(job)
    manager = QuotaManager(rig.store)
    reservation = manager.reserve(ready)

    released = manager.release(reservation.id, consumed=14)

    assert reservation.amount == 10
    assert released.state is ReservationState.RELEASED
    assert released.consumed == 14
    assert rig.plane.inspect_quota("default").remaining == 86
    assert rig.plane.inspect_quota("default").reserved == 0
    assert manager.release(reservation.id, consumed=14) == released
    assert rig.plane.inspect_quota("default").remaining == 86


def test_duplicate_registration_preserves_live_resource_counters(
    make_regression_rig,
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    rig = make_regression_rig()
    live_node = replace(
        regression_node,
        allocated=ResourceVector(cpu=3, ram_gb=7),
    )
    live_pool = replace(regression_pool, remaining=73, reserved=11)
    rig.store.save_node(live_node)
    rig.store.save_quota_pool(live_pool)

    registered_node = rig.plane.register_node(
        replace(
            regression_node,
            labels={"os": "linux", "arch": "x86_64", "zone": "updated"},
            capacity=ResourceVector(cpu=16, ram_gb=64),
            allocated=ResourceVector(0, 0),
        )
    )
    registered_pool = rig.plane.register_quota_pool(
        replace(
            regression_pool,
            provider="updated-provider",
            remaining=100,
            reserved=0,
        )
    )

    assert registered_node.capacity == ResourceVector(cpu=16, ram_gb=64)
    assert registered_node.labels["zone"] == "updated"
    assert registered_node.allocated == live_node.allocated
    assert rig.store.get_node(regression_node.id).allocated == live_node.allocated
    assert registered_pool.provider == "updated-provider"
    assert registered_pool.remaining == live_pool.remaining
    assert registered_pool.reserved == live_pool.reserved
    assert rig.plane.inspect_quota(regression_pool.id) == registered_pool
