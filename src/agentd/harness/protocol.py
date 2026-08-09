"""Capability-driven interface implemented by every harness adapter."""

from typing import Protocol, runtime_checkable

from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
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
