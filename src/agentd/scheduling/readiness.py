"""Pure dependency, synchronization-barrier, and gang readiness policies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from agentd.domain.enums import JobState
from agentd.domain.models import Job


@dataclass(frozen=True, slots=True)
class Readiness:
    """A readiness decision with stable identifiers for audit messages."""

    ready: bool
    blockers: tuple[str, ...] = ()


def dependency_readiness(
    job: Job,
    states: Mapping[str, JobState],
) -> Readiness:
    """Require every declared dependency to exist and be completed."""

    blockers = tuple(
        dependency_id
        for dependency_id in job.dependencies
        if states.get(dependency_id) is not JobState.COMPLETED
    )
    return Readiness(ready=not blockers, blockers=blockers)


def barrier_readiness(
    member_ids: Iterable[str],
    states: Mapping[str, JobState],
) -> Readiness:
    """Model an MVP barrier as all listed members having completed.

    Missing members are blockers. Duplicate member IDs are reported once in the
    order in which the barrier declared them.
    """

    unique_members = tuple(dict.fromkeys(member_ids))
    blockers = tuple(
        member_id
        for member_id in unique_members
        if states.get(member_id) is not JobState.COMPLETED
    )
    return Readiness(ready=not blockers, blockers=blockers)


def gang_readiness(
    job: Job,
    jobs: Iterable[Job],
    states: Mapping[str, JobState],
) -> Readiness:
    """Require every member of a dispatch gang to be dependency-ready.

    This is the useful MVP gang semantic: no member is admitted until every
    known member could be admitted. It does not claim atomic multi-node launch;
    that remains a coordinator concern. Jobs without a gang use ordinary
    dependency readiness.
    """

    if job.gang_id is None:
        return dependency_readiness(job, states)

    members = sorted(
        (candidate for candidate in jobs if candidate.gang_id == job.gang_id),
        key=lambda candidate: candidate.id,
    )
    if all(member.id != job.id for member in members):
        members.append(job)
        members.sort(key=lambda candidate: candidate.id)

    blockers: list[str] = []
    for member in members:
        blockers.extend(dependency_readiness(member, states).blockers)
    stable_blockers = tuple(dict.fromkeys(blockers))
    return Readiness(ready=not stable_blockers, blockers=stable_blockers)
