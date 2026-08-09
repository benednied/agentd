"""Explicit, auditable job lifecycle transitions."""

from dataclasses import replace
from datetime import datetime

from agentd.domain.enums import JobState
from agentd.domain.models import Job, StateTransition, utc_now


class InvalidStateTransition(ValueError):
    """Raised when a caller attempts to violate the job state machine."""


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.BACKLOG: frozenset(
        {JobState.PLANNING, JobState.READY, JobState.CANCELLED}
    ),
    JobState.PLANNING: frozenset(
        {JobState.READY, JobState.BACKLOG, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.READY: frozenset(
        {JobState.ADMITTED, JobState.BACKLOG, JobState.CANCELLED}
    ),
    JobState.ADMITTED: frozenset(
        {JobState.RUNNING, JobState.READY, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.DRAINING,
            JobState.CHECKPOINTED,
            JobState.METERING_PENDING,
            JobState.REVIEW,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.DRAINING: frozenset(
        {
            JobState.RUNNING,
            JobState.CHECKPOINTED,
            JobState.METERING_PENDING,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.CHECKPOINTED: frozenset(
        {
            JobState.RUNNING,
            JobState.METERING_PENDING,
            JobState.SUSPENDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.METERING_PENDING: frozenset(
        {
            JobState.REVIEW,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.SUSPENDED: frozenset(
        {JobState.READY, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.REVIEW: frozenset(
        {
            JobState.RUNNING,
            JobState.METERING_PENDING,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def can_transition(from_state: JobState, to_state: JobState) -> bool:
    return to_state in ALLOWED_TRANSITIONS[from_state]


def transition_job(
    job: Job,
    to_state: JobState,
    reason: str,
    *,
    at: datetime | None = None,
) -> tuple[Job, StateTransition]:
    """Return the updated aggregate and its audit event, or reject the transition."""

    if not reason.strip():
        raise ValueError("A state transition requires a reason")
    if not can_transition(job.state, to_state):
        raise InvalidStateTransition(
            f"Job {job.id} cannot transition from {job.state} to {to_state}"
        )
    occurred_at = at or utc_now()
    updated = replace(job, state=to_state, updated_at=occurred_at)
    event = StateTransition(
        job_id=job.id,
        from_state=job.state,
        to_state=to_state,
        reason=reason,
        occurred_at=occurred_at,
    )
    return updated, event


def initial_transition(job: Job, *, reason: str = "job submitted") -> StateTransition:
    return StateTransition(
        job_id=job.id,
        from_state=None,
        to_state=job.state,
        reason=reason,
        occurred_at=job.created_at,
    )
