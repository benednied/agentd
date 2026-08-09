"""Harness driver backed by the official Codex Python SDK/App Server."""

from __future__ import annotations

from collections.abc import Callable

from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunObservation,
    RunResult,
    new_id,
)
from agentd.harness.app_server import DEFAULT_CODEX_MODEL
from agentd.harness.errors import UnknownRunError
from agentd.harness.supervisor import (
    CODEX_DRIVER_NAME,
    DEFAULT_RECOVERY_INSTRUCTION,
    RunSupervisor,
)


class CodexSdkDriver:
    """Translate harness lifecycle calls into durable App Server operations."""

    def __init__(
        self,
        supervisor: RunSupervisor,
        *,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._supervisor = supervisor
        self._id_factory = id_factory
        self._capabilities = HarnessCapabilities(
            name=CODEX_DRIVER_NAME,
            # ``standard`` remains the scheduler's abstract compatibility
            # class; every dispatched turn uses the concrete model below.
            models=frozenset({"standard", DEFAULT_CODEX_MODEL}),
            features=frozenset(
                {
                    "checkpointing",
                    "live-token-usage",
                    "restartable-threads",
                    "restricted-workspace-write",
                    "streaming",
                    "structured-output",
                    "steering",
                }
            ),
            native_pause=False,
            steering=True,
            checkpointing=True,
        )

    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    async def start(self, execution: ExecutionContract) -> RunHandle:
        """Compatibility entry point; managed runtimes should supply their run ID."""

        return await self.start_managed(self._id_factory(), execution)

    async def start_managed(
        self,
        run_id: str,
        execution: ExecutionContract,
    ) -> RunHandle:
        return await self._supervisor.start(run_id, execution)

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = DEFAULT_RECOVERY_INSTRUCTION,
    ) -> RunHandle:
        return await self._supervisor.recover(
            run_id,
            execution,
            recovery_instruction,
        )

    def observe(self, run_id: str) -> RunObservation | None:
        return self._supervisor.observe(run_id)

    async def process_pending(self, run_id: str) -> None:
        await self._supervisor.process_pending(run_id)

    async def continue_turn(self, run_id: str, instruction: str) -> RunHandle:
        return await self._supervisor.continue_turn(run_id, instruction)

    async def steer(self, run: RunHandle, instruction: str) -> None:
        self._validate_handle(run)
        await self._supervisor.steer(run.id, instruction)

    async def interrupt(self, run: RunHandle) -> None:
        self._validate_handle(run)
        await self._supervisor.interrupt(run.id)

    async def collect(self, run: RunHandle) -> RunResult:
        self._validate_handle(run)
        return await self._supervisor.collect(run.id)

    async def cancel(self, run: RunHandle) -> None:
        self._validate_handle(run)
        await self._supervisor.cancel(run.id)

    async def close(self) -> None:
        await self._supervisor.close()

    @staticmethod
    def _validate_handle(run: RunHandle) -> None:
        if run.driver != CODEX_DRIVER_NAME:
            raise UnknownRunError(
                f"Run {run.id!r} belongs to driver {run.driver!r}, "
                f"not {CODEX_DRIVER_NAME!r}"
            )
