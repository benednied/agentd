from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import JobState
from agentd.domain.models import Job, QuotaPool, StateTransition, WorkerNode
from agentd.domain.transitions import InvalidStateTransition, initial_transition
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore


def test_missing_quota_pool_does_not_block_another_runnable_job(
    make_regression_rig,
    make_regression_job: Callable[..., Job],
    regression_node: WorkerNode,
    regression_pool: QuotaPool,
) -> None:
    rig = make_regression_rig()
    missing_pool_job = make_regression_job(
        id="a-missing-pool",
        quota_budget=replace(
            make_regression_job().quota_budget,
            pool_id="missing",
        ),
    )
    runnable_job = make_regression_job(id="b-runnable")

    async def scenario() -> None:
        rig.plane.register_node(regression_node)
        rig.plane.register_quota_pool(regression_pool)
        rig.plane.submit(missing_pool_job)
        rig.plane.submit(runnable_job)

        run = await rig.plane.dispatch_next()

        assert run is not None
        assert run.job_id == runnable_job.id
        assert rig.plane.inspect_job(missing_pool_job.id).state is JobState.READY
        assert rig.store.list_reservations(missing_pool_job.id) == []
        assert rig.store.list_allocations(missing_pool_job.id) == []
        assert rig.store.list_workspaces(missing_pool_job.id) == []

    asyncio.run(scenario())


def test_sqlite_rejects_caller_forged_invalid_transition(
    make_regression_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    job = make_regression_job()
    initial = initial_transition(job)
    store.create_job(job, initial)
    forged_job = replace(job, state=JobState.COMPLETED)
    forged_transition = StateTransition(
        id="forged-transition",
        job_id=job.id,
        from_state=JobState.BACKLOG,
        to_state=JobState.COMPLETED,
        reason="caller attempted to bypass the state machine",
        occurred_at=job.created_at,
    )

    try:
        with pytest.raises((ConcurrentStateError, InvalidStateTransition)):
            store.save_job(forged_job, forged_transition)

        assert store.get_job(job.id) == job
        assert store.list_transitions(job.id) == [initial]
    finally:
        store.close()
