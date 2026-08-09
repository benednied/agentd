from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agentd.domain.enums import JobState
from agentd.domain.models import Job, QuotaPool, WorkerNode


def test_review_requires_explicit_acceptance(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    rig = make_application_rig()
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)
        run = await rig.plane.dispatch_next()
        assert run is not None

        review = await rig.plane.request_review(job.id)

        assert review.state is JobState.REVIEW
        assert rig.plane.inspect_job(job.id).state is JobState.REVIEW
        accepted = await rig.plane.accept(job.id)
        assert accepted.state is JobState.COMPLETED

    asyncio.run(scenario())


def test_accept_rejects_job_outside_review(
    make_application_rig,
    make_application_job: Callable[..., Job],
) -> None:
    rig = make_application_rig()
    job = make_application_job()
    rig.plane.submit(job)

    with pytest.raises(ValueError, match="not awaiting review"):
        asyncio.run(rig.plane.accept(job.id))
