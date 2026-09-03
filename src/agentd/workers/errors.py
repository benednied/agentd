"""Errors raised by worker backends and their registry."""


class WorkerBackendError(RuntimeError):
    """Base class for worker-backend failures."""


class WorkerProtocolError(WorkerBackendError, ValueError):
    """Raised when a remote worker message violates the wire contract."""


class WorkerAuthenticationError(WorkerProtocolError, PermissionError):
    """Raised when a remote message cannot be authenticated safely."""


class WorkerTransportError(WorkerBackendError, ConnectionError):
    """Raised when a remote connection fails before a response is received."""


class WorkerStartUncertainError(WorkerTransportError):
    """A remote START may have reached the worker but its result is unknown.

    The coordinator must retain the durable STARTING intent and reservation;
    retrying under a new run id could duplicate an already-created process.
    """


class WorkerOperationError(WorkerBackendError):
    """Raised when a typed worker operation is rejected or fails."""


class WorkerJournalConflictError(WorkerOperationError):
    """Raised when a request id is reused with a different operation payload."""


class BackendAlreadyRegisteredError(WorkerBackendError):
    """Raised when a registry entry would be replaced accidentally."""


class UnknownBackendError(WorkerBackendError, LookupError):
    """Raised when a requested worker backend is not registered."""
