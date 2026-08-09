"""Local-process worker backend."""

from __future__ import annotations

import platform

from agentd.domain.enums import NodeState
from agentd.domain.models import ExecutionContract, RunHandle, WorkerNode
from agentd.harness.protocol import HarnessDriver
from agentd.workers.protocol import WorkerBackendCapabilities

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
    ) -> RunHandle:
        """Start locally without adding harness-selection policy."""

        return await driver.start(contract)
