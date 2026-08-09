"""Worker-node resource accounting."""

from dataclasses import replace

from agentd.domain.enums import AllocationState, NodeState
from agentd.domain.models import Job, ResourceAllocation, WorkerNode, utc_now
from agentd.state.base import ConcurrentStateError, StateStore

_MAX_OPTIMISTIC_ATTEMPTS = 8


class ResourceAllocationError(RuntimeError):
    pass


class ResourceManager:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    def allocate(self, job: Job, node: WorkerNode) -> ResourceAllocation:
        conflict: ConcurrentStateError | None = None
        for _attempt in range(_MAX_OPTIMISTIC_ATTEMPTS):
            existing = self._store.find_active_allocation(job.id)
            if existing is not None:
                self._validate_existing(job, node, existing)
                return existing
            current = self._store.get_node(node.id)
            if current.state is not NodeState.ONLINE:
                raise ResourceAllocationError(f"Node {node.id} is not online")
            if not job.resources.fits_within(current.available):
                raise ResourceAllocationError(
                    f"Node {node.id} no longer has enough resources for job {job.id}"
                )
            updated = replace(
                current,
                allocated=current.allocated + job.resources,
                updated_at=utc_now(),
            )
            allocation = ResourceAllocation(
                job_id=job.id,
                node_id=node.id,
                resources=job.resources,
            )
            try:
                self._store.allocate_resources(current, updated, allocation)
            except ConcurrentStateError as error:
                conflict = error
                continue
            return allocation

        raise ConcurrentStateError(
            f"Could not allocate resources for job {job.id} after concurrent updates"
        ) from conflict

    def release(self, allocation_id: str) -> ResourceAllocation:
        conflict: ConcurrentStateError | None = None
        for _attempt in range(_MAX_OPTIMISTIC_ATTEMPTS):
            allocation = self._store.get_allocation(allocation_id)
            if allocation.state != AllocationState.ACTIVE:
                return allocation
            node = self._store.get_node(allocation.node_id)
            if not allocation.resources.fits_within(node.allocated):
                raise ConcurrentStateError(
                    f"Node {node.id} does not account for allocation {allocation.id}"
                )
            now = utc_now()
            updated = replace(
                node,
                allocated=node.allocated - allocation.resources,
                updated_at=now,
            )
            released = replace(
                allocation,
                state=AllocationState.RELEASED,
                released_at=now,
            )
            try:
                self._store.release_resources(node, updated, allocation, released)
            except ConcurrentStateError as error:
                conflict = error
                continue
            return released

        raise ConcurrentStateError(
            f"Could not release allocation {allocation_id} after concurrent updates"
        ) from conflict

    @staticmethod
    def _validate_existing(
        job: Job,
        node: WorkerNode,
        allocation: ResourceAllocation,
    ) -> None:
        if (
            allocation.job_id != job.id
            or allocation.node_id != node.id
            or allocation.resources != job.resources
        ):
            raise ResourceAllocationError(
                f"Job {job.id} already has an incompatible active allocation"
            )
