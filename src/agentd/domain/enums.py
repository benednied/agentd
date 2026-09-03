"""Stable domain enumerations.

These values are persisted. Renaming a value is therefore a schema migration, not
just a refactor.
"""

from enum import StrEnum


class QoSClass(StrEnum):
    INTERACTIVE = "interactive"
    BLOCKER = "blocker"
    COMMITTED = "committed"
    NORMAL = "normal"
    SPECULATIVE = "speculative"
    SCAVENGER = "scavenger"
    HORS_CATEGORIE = "hors-categorie"


class JobState(StrEnum):
    BACKLOG = "BACKLOG"
    PLANNING = "PLANNING"
    READY = "READY"
    ADMITTED = "ADMITTED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    CHECKPOINTED = "CHECKPOINTED"
    METERING_PENDING = "METERING_PENDING"
    SUSPENDED = "SUSPENDED"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobDisposition(StrEnum):
    RUN = "run"
    THROTTLE = "throttle"
    SUSPEND = "suspend"
    RESUME = "resume"
    CANCEL = "cancel"
    DEGRADE = "degrade"
    BACKLOG = "backlog"


class PreemptionPolicy(StrEnum):
    NEVER = "never"
    CHECKPOINT = "checkpoint"
    TURN_BOUNDARY = "turn-boundary"


class CheckpointPolicy(StrEnum):
    NONE = "none"
    ON_REQUEST = "on-request"
    PERIODIC = "periodic"
    TURN_BOUNDARY = "turn-boundary"


class QuotaMode(StrEnum):
    NORMAL = "NORMAL"
    RESET_ANNOUNCED = "RESET_ANNOUNCED"
    PRE_RESET_BURN = "PRE_RESET_BURN"
    RESET_CONFIRMED = "RESET_CONFIRMED"
    EMERGENCY_CONSERVE = "EMERGENCY_CONSERVE"


class ReservationState(StrEnum):
    ACTIVE = "ACTIVE"
    METERING_PENDING = "METERING_PENDING"
    RELEASED = "RELEASED"
    CANCELLED = "CANCELLED"


class QuotaUnit(StrEnum):
    """Dimension used by a quota pool and its normalized usage samples."""

    ABSTRACT = "abstract"
    TOKENS = "tokens"


class ArtifactKind(StrEnum):
    """Immutable artifact reference families supported by the control plane."""

    GIT_COMMIT = "git_commit"
    OCI_IMAGE = "oci_image"


class OperationKind(StrEnum):
    """Typed, non-shell operations a worker may execute."""

    BUILD_IMAGE = "build_image"
    DEPLOY_IMAGE = "deploy_image"


class AgentRequestKind(StrEnum):
    """Worker-to-control-plane communication record types."""

    REFINEMENT = "refinement"
    BLOCKER = "blocker"


class WorkspaceState(StrEnum):
    LEASED = "LEASED"
    RETAINED = "RETAINED"
    RELEASED = "RELEASED"
    FAILED = "FAILED"


class NodeState(StrEnum):
    ONLINE = "ONLINE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class AllocationState(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


class RunState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    CHECKPOINTED = "CHECKPOINTED"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TailAction(StrEnum):
    CONTINUE = "continue"
    REESTIMATE = "reestimate"
    CHECKPOINT_REPLAN = "checkpoint-replan"
    CONVERT_TO_HORS_CATEGORIE = "convert-to-hors-categorie"


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
