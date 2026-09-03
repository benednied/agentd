"""Local-process worker backend."""

from __future__ import annotations

import platform
from inspect import isawaitable

from agentd.domain.enums import NodeState
from agentd.domain.models import (
    ExecutionContract,
    RunHandle,
    RunObservation,
    RunResult,
    WorkerNode,
)
from agentd.harness.protocol import HarnessDriver, ManagedHarnessDriver
from agentd.workers.protocol import (
    WorkerBackendCapabilities,
    validate_status_payload,
)

_OS_ALIASES = {
    "darwin": "macos",
    "mac": "macos",
    "macosx": "macos",
    "osx": "macos",
    "win32": "windows",
    "win64": "windows",
}
_ARCHITECTURE_ALIASES = {
    "aarch64": "arm64",
    "amd64": "x86_64",
    "x64": "x86_64",
}


def _normalized_os(value: str) -> str:
    normalized = value.strip().casefold()
    return _OS_ALIASES.get(normalized, normalized)


def _normalized_architecture(value: str) -> str:
    normalized = value.strip().casefold()
    return _ARCHITECTURE_ALIASES.get(normalized, normalized)


class LocalWorkerBackend:
    """Dispatch contracts directly through a harness on the current machine.

    ``node_id`` can bind the backend to one registered node. Without it, an
    online node is considered local when its optional ``backend``, ``os``, and
    ``arch`` labels agree with this process.
    """

    def __init__(
        self,
        *,
        name: str = "local",
        node_id: str | None = None,
        operating_system: str | None = None,
        architecture: str | None = None,
    ) -> None:
        current_os = _normalized_os(operating_system or platform.system())
        current_architecture = _normalized_architecture(
            architecture or platform.machine()
        )
        self._node_id = node_id
        self._drivers: dict[str, HarnessDriver] = {}
        self._handles: dict[str, RunHandle] = {}
        self._capabilities = WorkerBackendCapabilities(
            name=name,
            supported_operating_systems=frozenset({current_os}),
            supported_architectures=frozenset({current_architecture}),
            features=frozenset({"local-process"}),
            remote=False,
        )

    def capabilities(self) -> WorkerBackendCapabilities:
        return self._capabilities

    def is_compatible(self, node: WorkerNode) -> bool:
        if node.state is not NodeState.ONLINE:
            return False
        if self._node_id is not None and node.id != self._node_id:
            return False

        backend_label = node.labels.get("backend")
        if backend_label is not None and backend_label != self._capabilities.name:
            return False

        operating_system = node.labels.get("os")
        if operating_system is not None and (
            _normalized_os(operating_system)
            not in self._capabilities.supported_operating_systems
        ):
            return False

        architecture = node.labels.get("arch")
        return architecture is None or (
            _normalized_architecture(architecture)
            in self._capabilities.supported_architectures
        )

    async def dispatch(
        self,
        driver: HarnessDriver,
        contract: ExecutionContract,
        *,
        run_id: str | None = None,
        managed: bool = False,
    ) -> RunHandle:
        """Start locally without adding harness-selection policy."""

        if managed:
            if not isinstance(driver, ManagedHarnessDriver):
                raise TypeError(
                    f"Driver {driver.capabilities().name!r} does not support "
                    "managed starts"
                )
            handle = await driver.start_managed(run_id or contract.job_id, contract)
        else:
            handle = await driver.start(contract)
        self._drivers[handle.id] = driver
        self._handles[handle.id] = handle
        if run_id is not None:
            self._drivers[run_id] = driver
            self._handles[run_id] = handle
        return handle

    async def observe(self, run_id: str) -> RunObservation | None:
        driver = self._drivers.get(run_id)
        if not isinstance(driver, ManagedHarnessDriver):
            return None
        return driver.observe(run_id)

    async def status(self, run: RunHandle | str) -> dict[str, object]:
        run_id = run if isinstance(run, str) else run.id
        driver = self._drivers.get(run_id)
        if driver is None:
            return {"known": False, "terminal": False, "result": None}
        status_method = getattr(driver, "status", None)
        if status_method is None:
            return {"known": True, "terminal": False, "result": None}
        handle = run if isinstance(run, RunHandle) else self._handles[run]
        raw_status = status_method(handle)
        if isawaitable(raw_status):
            raw_status = await raw_status
        return validate_status_payload(raw_status)

    async def steer(self, run: RunHandle | str, instruction: str) -> None:
        driver, handle = self._driver_and_handle(run)
        await driver.steer(handle, instruction)

    async def interrupt(self, run: RunHandle | str) -> None:
        driver, handle = self._driver_and_handle(run)
        await driver.interrupt(handle)

    async def cancel(self, run: RunHandle | str) -> None:
        driver, handle = self._driver_and_handle(run)
        await driver.cancel(handle)

    async def collect(self, run: RunHandle | str) -> RunResult:
        driver, handle = self._driver_and_handle(run)
        return await driver.collect(handle)

    async def heartbeat(self) -> dict[str, object]:
        return {
            "node_id": self._node_id,
            "backend": self._capabilities.name,
            "remote": False,
        }

    async def close(self) -> None:
        # Harness drivers are owned by the enclosing local runtime.  There is
        # no transport to close here, so this is intentionally a no-op.
        return None

    def _driver_and_handle(
        self,
        run: RunHandle | str,
    ) -> tuple[HarnessDriver, RunHandle]:
        run_id = run if isinstance(run, str) else run.id
        try:
            driver = self._drivers[run_id]
            handle = run if isinstance(run, RunHandle) else self._handles[run_id]
            return driver, handle
        except KeyError as error:
            raise KeyError(f"Unknown local worker run {run_id!r}") from error
