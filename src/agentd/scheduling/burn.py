"""Quota-mode policy for useful, checkpointable pre-reset burn work."""

from collections.abc import Iterable

from agentd.domain.enums import CheckpointPolicy, JobState, QoSClass, QuotaMode
from agentd.domain.models import Job
from agentd.scheduling.priority import PriorityKey, priority_key

_URGENT_QOS = frozenset({QoSClass.INTERACTIVE, QoSClass.BLOCKER})

BurnOrderKey = tuple[int, PriorityKey]


def declares_burn_eligibility(job: Job) -> bool:
    """Return whether a bounded job declares a usable burn contract."""

    return (
        job.burn.eligible
        and job.burn.checkpointable
        and job.checkpoint_policy is not CheckpointPolicy.NONE
        and job.qos is not QoSClass.HORS_CATEGORIE
    )


def is_burn_candidate(job: Job, mode: QuotaMode) -> bool:
    """Return whether the scheduler may treat a ready job as burn work now."""

    return (
        mode is QuotaMode.PRE_RESET_BURN
        and job.state is JobState.READY
        and declares_burn_eligibility(job)
    )


def burn_order_key(job: Job, mode: QuotaMode) -> BurnOrderKey:
    """Return a mode-aware key while preserving urgent interactive capacity."""

    if mode is not QuotaMode.PRE_RESET_BURN or job.qos in _URGENT_QOS:
        group = 0
    elif is_burn_candidate(job, mode):
        group = 1
    else:
        group = 2
    return group, priority_key(job)


def order_jobs_for_quota_mode(
    jobs: Iterable[Job],
    mode: QuotaMode,
) -> tuple[Job, ...]:
    """Order candidates normally or with pre-reset burn promotion."""

    return tuple(sorted(jobs, key=lambda job: burn_order_key(job, mode)))
