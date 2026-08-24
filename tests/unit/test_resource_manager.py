from collections.abc import Callable

from agentd.domain.enums import AllocationState
from agentd.domain.models import Job, ResourceVector, WorkerNode
from agentd.domain.transitions import initial_transition
from agentd.runtime.resources import ResourceManager
from agentd.state.sqlite import SQLiteStateStore


def test_resource_allocation_and_release_are_idempotent(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(resources=ResourceVector(cpu=2, ram_gb=4))
    node = WorkerNode(
        id="local",
        labels={"os": "linux", "arch": "x86_64"},
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset({"fake"}),
    )
    store = SQLiteStateStore()
    store.create_job(job, initial_transition(job))
    store.save_node(node)
    manager = ResourceManager(store)

    allocation = manager.allocate(job, node)
    same = manager.allocate(job, node)
    released = manager.release(allocation.id)
    released_again = manager.release(allocation.id)

    assert same == allocation
    assert released.state == AllocationState.RELEASED
    assert released_again == released
    assert store.get_node(node.id).allocated == ResourceVector(0, 0)
