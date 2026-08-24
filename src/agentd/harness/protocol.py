"""Capability-driven interface implemented by every harness adapter."""

from typing import Protocol, runtime_checkable

from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunObservation,
    RunResult,
)


@runtime_checkable
class HarnessDriver(Protocol):
    """Translate generic execution contracts into harness-specific operations."""

    def capabilities(self) -> HarnessCapabilities: ...

    async def start(self, execution: ExecutionContract) -> RunHandle: ...

    async def steer(self, run: RunHandle, instruction: str) -> None: ...

    async def interrupt(self, run: RunHandle) -> None: ...

    async def collect(self, run: RunHandle) -> RunResult: ...

    async def cancel(self, run: RunHandle) -> None: ...


@runtime_checkable
class ManagedHarnessDriver(HarnessDriver, Protocol):
    """Durable, observable driver used by restartable service runtimes."""

    async def start_managed(
        self,
        run_id: str,
        execution: ExecutionContract,
    ) -> RunHandle: ...

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = "Resume from the durable thread and workspace.",
    ) -> RunHandle: ...

    def observe(self, run_id: str) -> RunObservation | None: ...


@runtime_checkable
class PendingCommandHarnessDriver(ManagedHarnessDriver, Protocol):
    """Managed driver capable of processing durable queued commands."""

    async def process_pending(self, run_id: str) -> None: ...


@runtime_checkable
class ContinuingManagedHarnessDriver(ManagedHarnessDriver, Protocol):
    """Managed driver capable of same-thread repair continuation."""

    async def continue_turn(self, run_id: str, instruction: str) -> RunHandle: ...
