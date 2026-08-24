from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from agentd.domain.enums import AllocationState, JobState, ReservationState
from agentd.domain.models import Job, QuotaPool, WorkerNode, WorkspaceLease
from agentd.provisioning import RepositoryProvisioningError


@dataclass(slots=True)
class RecordingProvisioner:
    error: Exception | None = None
    leases: list[WorkspaceLease] = field(default_factory=list)

    async def prepare(self, lease: WorkspaceLease) -> None:
        self.leases.append(lease)
        if self.error is not None:
            raise self.error


def test_dispatch_provisions_workspace_before_start(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    provisioner = RecordingProvisioner()
    rig = make_application_rig(provisioner=provisioner)
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)

        run = await rig.plane.dispatch_next()

        assert run is not None
        assert [lease.id for lease in provisioner.leases] == [run.workspace_id]
        assert rig.plane.inspect_job(job.id).state is JobState.RUNNING

    asyncio.run(scenario())


def test_failed_provisioning_compensates_without_starting_driver(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    provisioner = RecordingProvisioner(
        error=RepositoryProvisioningError("trusted sync failed")
    )
    rig = make_application_rig(provisioner=provisioner)
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)

        with pytest.raises(RepositoryProvisioningError, match="trusted sync failed"):
            await rig.plane.dispatch_next()

        assert rig.plane.inspect_job(job.id).state is JobState.READY
        assert len(provisioner.leases) == 1
        assert rig.store.find_active_allocation(job.id) is None
        assert rig.store.find_active_reservation(job.id) is None
        assert rig.store.list_allocations(job.id)[0].state is AllocationState.RELEASED
        assert rig.store.list_reservations(job.id)[0].state is (
            ReservationState.CANCELLED
        )
        assert rig.workspaces.releases == [provisioner.leases[0].id]

    asyncio.run(scenario())
