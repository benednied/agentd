"""Worker execution-backend interfaces and built-in implementations."""

from agentd.workers.bootstrap import (
    OperationAllowlist,
    WorkerServeConfig,
    create_worker_server,
    run_worker_server,
)
from agentd.workers.client import RemoteWorkerClient
from agentd.workers.controller import (
    MAX_REMOTE_WORKERS,
    MAX_REMOTE_WORKERS_CONFIG_BYTES,
    OperationsHarnessDescriptor,
    RemoteWorkerController,
    RemoteWorkerEndpoint,
)
from agentd.workers.errors import (
    BackendAlreadyRegisteredError,
    UnknownBackendError,
    WorkerAuthenticationError,
    WorkerBackendError,
    WorkerJournalConflictError,
    WorkerOperationError,
    WorkerProtocolError,
    WorkerStartUncertainError,
    WorkerTransportError,
)
from agentd.workers.execution import ExecutionService, OperationResponse
from agentd.workers.journal import JournalEntry, OperationJournal
from agentd.workers.local import LocalWorkerBackend
from agentd.workers.operations import OperationHarnessDriver
from agentd.workers.protocol import (
    ARTIFACT_VERIFICATION_FEATURE,
    WorkerBackend,
    WorkerBackendCapabilities,
)
from agentd.workers.registry import BackendRegistry
from agentd.workers.remote import RemoteWorkerBackend
from agentd.workers.server import WorkerServer

__all__ = [
    "ARTIFACT_VERIFICATION_FEATURE",
    "MAX_REMOTE_WORKERS",
    "MAX_REMOTE_WORKERS_CONFIG_BYTES",
    "BackendAlreadyRegisteredError",
    "BackendRegistry",
    "ExecutionService",
    "JournalEntry",
    "LocalWorkerBackend",
    "OperationAllowlist",
    "OperationHarnessDriver",
    "OperationJournal",
    "OperationResponse",
    "OperationsHarnessDescriptor",
    "RemoteWorkerBackend",
    "RemoteWorkerClient",
    "RemoteWorkerController",
    "RemoteWorkerEndpoint",
    "UnknownBackendError",
    "WorkerAuthenticationError",
    "WorkerBackend",
    "WorkerBackendCapabilities",
    "WorkerBackendError",
    "WorkerJournalConflictError",
    "WorkerOperationError",
    "WorkerProtocolError",
    "WorkerServeConfig",
    "WorkerServer",
    "WorkerStartUncertainError",
    "WorkerTransportError",
    "create_worker_server",
    "run_worker_server",
]
