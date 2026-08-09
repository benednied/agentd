"""Errors raised by harness adapters and their registry."""


class HarnessError(RuntimeError):
    """Base class for harness integration failures."""


class DriverAlreadyRegisteredError(HarnessError):
    """Raised when a registry name would be replaced accidentally."""


class UnknownDriverError(HarnessError, LookupError):
    """Raised when a requested harness driver is not registered."""


class UnknownRunError(HarnessError, LookupError):
    """Raised when a run handle does not belong to a driver."""


class RunNotActiveError(HarnessError):
    """Raised when an operation requires a live harness run."""
