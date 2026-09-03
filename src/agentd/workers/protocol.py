"""Execution-backend capabilities and protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agentd.domain.models import (
    ExecutionContract,
    RunHandle,
    RunObservation,
    RunResult,
    WorkerNode,
)
from agentd.harness.protocol import HarnessDriver
from agentd.workers.errors import WorkerProtocolError

ARTIFACT_VERIFICATION_FEATURE = "artifact-verification"


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


def validate_status_payload(value: object) -> dict[str, object]:
    """Validate the common known/terminal worker status contract."""

    if not isinstance(value, dict):
        raise WorkerProtocolError("worker status is malformed")
    known = value.get("known")
    terminal = value.get("terminal")
    result = value.get("result")
    if not isinstance(known, bool) or not isinstance(terminal, bool):
        raise WorkerProtocolError("worker status flags are malformed")
    if not known and (terminal or result is not None):
        raise WorkerProtocolError("unknown worker status cannot be terminal")
    if not terminal and result is not None:
        raise WorkerProtocolError("non-terminal worker status has a result")
    if result is not None and not isinstance(result, dict):
        raise WorkerProtocolError("worker status result is malformed")
    if result is None:
        return value
    # Parsing through the domain type is intentional: accepting an arbitrary
    # mapping here would let malformed terminal results reach reconciliation,
    # where they could strand admission resources or publish bad artifacts.
    try:
        parsed = RunResult.from_dict(result)
    except Exception as error:
        raise WorkerProtocolError("worker status result is malformed") from error
    normalized = dict(value)
    normalized["result"] = parsed.to_dict()
    return normalized


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
        *,
        run_id: str | None = None,
        managed: bool = False,
    ) -> RunHandle:
        """Start ``contract`` through ``driver`` and return its run handle."""
        ...

    async def status(self, run: RunHandle | str) -> dict[str, object]:
        """Return an authenticated/read-only status for one run.

        ``known=False`` means the worker no longer has durable knowledge of
        the run (for example after a restart).  Such a status is never treated
        as a terminal success by the coordinator.
        """
        ...

    async def observe(self, run_id: str) -> RunObservation | None:
        """Return one current managed-run observation, when supported."""
        ...

    async def steer(self, run: RunHandle, instruction: str) -> None:
        """Deliver a bounded instruction to a running worker."""
        ...

    async def interrupt(self, run: RunHandle) -> None:
        """Ask a worker to stop at its next safe boundary."""
        ...

    async def cancel(self, run: RunHandle) -> None:
        """Cancel a worker run idempotently."""
        ...

    async def collect(self, run: RunHandle) -> RunResult:
        """Collect one terminal worker result."""
        ...

    async def heartbeat(self) -> dict[str, object]:
        """Return a worker liveness snapshot without inventing capacity."""
        ...

    async def close(self) -> None:
        """Close backend-owned transport resources."""
        ...
