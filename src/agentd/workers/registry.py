"""Registry for capability-discoverable worker backends."""

from collections.abc import Iterable

from agentd.domain.models import WorkerNode
from agentd.workers.errors import (
    BackendAlreadyRegisteredError,
    UnknownBackendError,
)
from agentd.workers.protocol import WorkerBackend, WorkerBackendCapabilities


class BackendRegistry:
    """Own backend discovery without choosing a harness, model, or node."""

    def __init__(self, backends: Iterable[WorkerBackend] = ()) -> None:
        self._backends: dict[str, WorkerBackend] = {}
        for backend in backends:
            self.register(backend)

    def register(self, backend: WorkerBackend, *, replace: bool = False) -> None:
        name = backend.capabilities().name
        if name in self._backends and not replace:
            raise BackendAlreadyRegisteredError(
                f"Worker backend {name!r} is already registered"
            )
        self._backends[name] = backend

    def get(self, name: str) -> WorkerBackend:
        try:
            return self._backends[name]
        except KeyError as error:
            raise UnknownBackendError(
                f"Worker backend {name!r} is not registered"
            ) from error

    def remove(self, name: str) -> WorkerBackend:
        try:
            return self._backends.pop(name)
        except KeyError as error:
            raise UnknownBackendError(
                f"Worker backend {name!r} is not registered"
            ) from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    def capabilities(self) -> tuple[WorkerBackendCapabilities, ...]:
        return tuple(self._backends[name].capabilities() for name in self.names())

    def compatible(self, node: WorkerNode) -> tuple[WorkerBackend, ...]:
        """Return compatible backends in deterministic registry-name order."""

        return tuple(
            self._backends[name]
            for name in self.names()
            if self._backends[name].is_compatible(node)
        )

    def __contains__(self, name: object) -> bool:
        return name in self._backends

    def __len__(self) -> int:
        return len(self._backends)
