from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import JobState, RunState
from agentd.domain.models import (
    ExecutionContract,
    Job,
    QuotaPool,
    RunHandle,
    RunObservation,
    RunRecord,
    WorkerNode,
)
from agentd.domain.transitions import transition_job
from agentd.harness import FakeHarnessDriver
from agentd.runtime.quota import QuotaManager
from agentd.runtime.resources import ResourceManager


class ManagedFakeDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "managed-handle")
        self.managed_run_ids: list[str] = []
        self.recovered_run_ids: list[str] = []

    async def start_managed(
        self,
        run_id: str,
        execution: ExecutionContract,
    ) -> RunHandle:
        self.managed_run_ids.append(run_id)
        return await super().start(execution)

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = "Resume from the durable thread and workspace.",
    ) -> RunHandle:
        self.recovered_run_ids.append(run_id)
        del recovery_instruction
        return await super().start(execution)

    def observe(self, run_id: str) -> RunObservation | None:
        del run_id
        return None


def test_managed_driver_receives_durable_run_id(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    driver = ManagedFakeDriver()
    rig = make_application_rig(driver=driver)
    job = make_application_job()

    async def scenario() -> None:
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)

        run = await rig.plane.dispatch_next()

        assert run is not None
        assert driver.managed_run_ids == [run.id]
        assert run.handle.id == "managed-handle"

    asyncio.run(scenario())


class RecoveryProvisioner:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    async def prepare(self, lease) -> None:
        self.workspace_ids.append(lease.id)


def _persist_starting_intent(rig, job: Job, node: WorkerNode) -> RunRecord:
    ready = rig.plane.submit(job)
    reservation = QuotaManager(rig.store).reserve(ready)
    allocation = ResourceManager(rig.store).allocate(ready, node)
    workspace = rig.workspaces.allocate(ready)
    rig.store.save_workspace(workspace, expected=None)
    selected = replace(
        ready,
        selected_harness="fake",
        selected_model_class="standard",
    )
    admitted, event = transition_job(selected, JobState.ADMITTED, "crash intent")
    rig.store.save_job(admitted, event, expected=ready)
    contract = ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="recovery test",
        acceptance_criteria=job.acceptance_criteria,
        dependency_results={},
        role="implementation worker",
        allowed_filesystem_scope=(workspace.working_directory,),
        checkpoint_expectations="return a checkpoint",
        coordination_mechanisms=("control-plane",),
        completion_protocol="return a review handoff",
        working_directory=workspace.working_directory,
        environment=workspace.environment,
        model_class="standard",
    )
    run = RunRecord(
        id="starting-managed-run",
        job_id=job.id,
        node_id=node.id,
        workspace_id=workspace.id,
        reservation_id=reservation.id,
        allocation_id=allocation.id,
        driver="fake",
        backend="direct",
        contract=contract,
        handle=RunHandle(id="pending-intent", driver="fake"),
        state=RunState.STARTING,
    )
    rig.store.save_run(run, expected=None)
    return run


def test_recovery_publishes_starting_handle_and_reruns_provisioning(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    driver = ManagedFakeDriver()
    provisioner = RecoveryProvisioner()
    rig = make_application_rig(driver=driver, provisioner=provisioner)
    rig.plane.register_node(application_node)
    rig.plane.register_quota_pool(application_quota_pool)
    run = _persist_starting_intent(rig, make_application_job(), application_node)

    asyncio.run(rig.plane.recover_managed_runs())

    assert provisioner.workspace_ids == [run.workspace_id]
    assert driver.recovered_run_ids == [run.id]
    assert rig.store.get_job(run.job_id).state is JobState.RUNNING
    recovered = rig.store.get_run(run.id)
    assert recovered.state is RunState.RUNNING
    assert recovered.handle.id == "managed-handle"


def test_recovery_rejects_workspace_ownership_change_before_transport(
    make_application_rig,
    make_application_job: Callable[..., Job],
    application_node: WorkerNode,
    application_quota_pool: QuotaPool,
) -> None:
    driver = ManagedFakeDriver()
    rig = make_application_rig(driver=driver)
    rig.plane.register_node(application_node)
    rig.plane.register_quota_pool(application_quota_pool)
    run = _persist_starting_intent(rig, make_application_job(), application_node)
    workspace = rig.store.get_workspace(run.workspace_id)
    rig.workspaces.leases[workspace.id] = replace(
        workspace,
        branch="agentd/replaced-owner",
    )

    with pytest.raises(RuntimeError, match="ownership or branch is unavailable"):
        asyncio.run(rig.plane.recover_managed_runs())

    assert driver.recovered_run_ids == []
    assert rig.store.get_job(run.job_id).state is JobState.ADMITTED
    assert rig.store.get_run(run.id).state is RunState.STARTING
