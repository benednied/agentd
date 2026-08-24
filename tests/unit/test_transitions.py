from collections.abc import Callable

import pytest

from agentd.domain.enums import JobState
from agentd.domain.models import Job
from agentd.domain.transitions import InvalidStateTransition, transition_job


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobState.BACKLOG, JobState.READY),
        (JobState.READY, JobState.ADMITTED),
        (JobState.ADMITTED, JobState.RUNNING),
        (JobState.RUNNING, JobState.DRAINING),
        (JobState.DRAINING, JobState.CHECKPOINTED),
        (JobState.CHECKPOINTED, JobState.SUSPENDED),
        (JobState.SUSPENDED, JobState.READY),
        (JobState.SUSPENDED, JobState.REVIEW),
        (JobState.RUNNING, JobState.REVIEW),
        (JobState.REVIEW, JobState.COMPLETED),
    ],
)
def test_allowed_transitions_are_auditable(
    make_job: Callable[..., Job], source: JobState, target: JobState
) -> None:
    job = make_job(state=source)

    updated, event = transition_job(job, target, "test transition")

    assert job.state == source
    assert updated.state == target
    assert event.job_id == job.id
    assert event.from_state == source
    assert event.to_state == target
    assert event.reason == "test transition"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobState.BACKLOG, JobState.RUNNING),
        (JobState.READY, JobState.COMPLETED),
        (JobState.SUSPENDED, JobState.RUNNING),
        (JobState.COMPLETED, JobState.READY),
        (JobState.CANCELLED, JobState.RUNNING),
    ],
)
def test_invalid_transitions_are_rejected_without_mutation(
    make_job: Callable[..., Job], source: JobState, target: JobState
) -> None:
    job = make_job(state=source)

    with pytest.raises(InvalidStateTransition):
        transition_job(job, target, "invalid transition")

    assert job.state == source


def test_transition_requires_reason(make_job: Callable[..., Job]) -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        transition_job(make_job(), JobState.READY, "  ")
