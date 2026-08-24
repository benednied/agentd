"""Execution-backend capabilities and protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agentd.domain.models import ExecutionContract, RunHandle, WorkerNode
from agentd.harness.protocol import HarnessDriver


@dataclass(frozen=True, slots=True)
class WorkerBackendCapabilities:
    """Stable metadata describing where and how a backend can execute work.

    Empty platform sets mean that the backend does not impose that constraint.
    Extensible feature names avoid coupling the interface to a particular
    remote transport, container runtime, or cluster scheduler.
    """

    name: str
    supported_operating_systems: frozenset[str] = frozenset()
    supported_architectures: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()
    remote: bool = False

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("A worker backend must declare a non-empty name")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "supported_operating_systems",
            frozenset(self.supported_operating_systems),
        )
        object.__setattr__(
            self,
            "supported_architectures",
            frozenset(self.supported_architectures),
        )
        object.__setattr__(self, "features", frozenset(self.features))


@runtime_checkable
class WorkerBackend(Protocol):
    """Dispatch harness work through one execution-environment mechanism."""

    def capabilities(self) -> WorkerBackendCapabilities:
        """Return immutable, scheduler-readable backend metadata."""
        ...

    def is_compatible(self, node: WorkerNode) -> bool:
        """Return whether this backend can address ``node``."""
        ...

    async def dispatch(
        self,
        driver: HarnessDriver,
        contract: ExecutionContract,
    ) -> RunHandle:
        """Start ``contract`` through ``driver`` and return its run handle."""
        ...
