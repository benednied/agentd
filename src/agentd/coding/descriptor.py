"""Capability descriptor for remote-only coding; never a local executor."""

from typing import Never

from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunObservation,
)
from agentd.workers.controller import OperationsHarnessDescriptor
from agentd.workers.errors import WorkerOperationError


class RemoteCodingDescriptor(OperationsHarnessDescriptor):
    def __init__(
        self, features: frozenset[str], models: frozenset[str] = frozenset({"standard"})
    ) -> None:
        self.features = features | {"remote-coding"}
        self.models = models

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            "remote-coding",
            self.models,
            self.features,
            steering=False,
            checkpointing=False,
        )

    def _fail(self) -> Never:
        raise WorkerOperationError("Remote coding cannot execute on the controller")

    async def start_managed(
        self, run_id: str, execution: ExecutionContract
    ) -> RunHandle:
        self._fail()

    async def recover(
        self, run_id: str, execution: ExecutionContract, recovery_instruction: str = ""
    ) -> RunHandle:
        self._fail()

    def observe(self, run_id: str) -> RunObservation | None:
        self._fail()
