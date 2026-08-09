"""Errors raised by worker backends and their registry."""


class WorkerBackendError(RuntimeError):
    """Base class for worker-backend failures."""


class BackendAlreadyRegisteredError(WorkerBackendError):
    """Raised when a registry entry would be replaced accidentally."""


class UnknownBackendError(WorkerBackendError, LookupError):
    """Raised when a requested worker backend is not registered."""
