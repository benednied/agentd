"""Deterministic quality-of-service and job-priority ordering."""

from collections.abc import Iterable
from datetime import datetime

from agentd.domain.enums import QoSClass
from agentd.domain.models import Job

_QOS_RANK: dict[QoSClass, int] = {
    QoSClass.INTERACTIVE: 0,
    QoSClass.BLOCKER: 1,
    QoSClass.COMMITTED: 2,
    QoSClass.NORMAL: 3,
    QoSClass.SPECULATIVE: 4,
    QoSClass.SCAVENGER: 5,
    # Hors-categorie work is normally compiled into a bounded reconnaissance
    # slice before dispatch. Ranking it last is a safe fallback for callers that
    # include it in a general scheduling view.
    QoSClass.HORS_CATEGORIE: 6,
}

PriorityKey = tuple[int, int, datetime, str]


def priority_key(job: Job) -> PriorityKey:
    """Return a stable ascending sort key for normal scheduler ordering.

    QoS is considered before the user-supplied integer priority. Higher integer
    priorities run first within a QoS class. Creation time and job ID provide a
    deterministic FIFO tie-break independent of input iteration order.
    """

    return (_QOS_RANK[job.qos], -job.priority, job.created_at, job.id)


def order_jobs(jobs: Iterable[Job]) -> tuple[Job, ...]:
    """Return jobs in deterministic normal scheduling order."""

    return tuple(sorted(jobs, key=priority_key))
