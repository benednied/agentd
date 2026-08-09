"""Worker execution-backend interfaces and built-in implementations."""

from agentd.workers.errors import (
    BackendAlreadyRegisteredError,
    UnknownBackendError,
    WorkerBackendError,
)
from agentd.workers.local import LocalWorkerBackend
from agentd.workers.protocol import WorkerBackend, WorkerBackendCapabilities
from agentd.workers.registry import BackendRegistry

__all__ = [
    "BackendAlreadyRegisteredError",
    "BackendRegistry",
    "LocalWorkerBackend",
    "UnknownBackendError",
    "WorkerBackend",
    "WorkerBackendCapabilities",
    "WorkerBackendError",
]
