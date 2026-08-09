from __future__ import annotations

import asyncio
from collections.abc import Callable

from agentd.domain.models import (
    ExecutionContract,
    Job,
    QuotaPool,
    RunHandle,
    RunObservation,
    WorkerNode,
)
from agentd.harness import FakeHarnessDriver


class ManagedFakeDriver(FakeHarnessDriver):
    def __init__(self) -> None:
        super().__init__(id_factory=lambda: "managed-handle")
        self.managed_run_ids: list[str] = []

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
        del run_id, recovery_instruction
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
