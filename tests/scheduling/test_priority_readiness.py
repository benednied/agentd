from datetime import UTC, datetime, timedelta

from agentd.domain.enums import JobState, QoSClass
from agentd.scheduling.priority import order_jobs
from agentd.scheduling.readiness import (
    barrier_readiness,
    dependency_readiness,
    gang_readiness,
)


def test_qos_precedes_integer_priority_and_hors_categorie_is_last(job_factory):
    jobs = [
        job_factory(id="hors", qos=QoSClass.HORS_CATEGORIE, priority=999),
        job_factory(id="normal", qos=QoSClass.NORMAL, priority=999),
        job_factory(id="blocker", qos=QoSClass.BLOCKER, priority=-10),
        job_factory(id="interactive", qos=QoSClass.INTERACTIVE, priority=-100),
        job_factory(id="scavenger", qos=QoSClass.SCAVENGER, priority=999),
        job_factory(id="committed", qos=QoSClass.COMMITTED),
        job_factory(id="speculative", qos=QoSClass.SPECULATIVE),
    ]

    assert [job.id for job in order_jobs(reversed(jobs))] == [
        "interactive",
        "blocker",
        "committed",
        "normal",
        "speculative",
        "scavenger",
        "hors",
    ]


def test_priority_then_fifo_then_id_are_stable_tie_breakers(job_factory):
    now = datetime(2026, 2, 1, tzinfo=UTC)
    jobs = [
        job_factory(id="b", priority=2, created_at=now),
        job_factory(id="late", priority=3, created_at=now + timedelta(seconds=1)),
        job_factory(id="a", priority=2, created_at=now),
        job_factory(id="early", priority=3, created_at=now),
    ]

    assert [job.id for job in order_jobs(jobs)] == ["early", "late", "a", "b"]


def test_dependency_readiness_requires_present_completed_dependencies(job_factory):
    job = job_factory(dependencies=("complete", "running", "missing"))
    decision = dependency_readiness(
        job,
        {"complete": JobState.COMPLETED, "running": JobState.RUNNING},
    )

    assert not decision.ready
    assert decision.blockers == ("running", "missing")
    assert dependency_readiness(
        job,
        {
            "complete": JobState.COMPLETED,
            "running": JobState.COMPLETED,
            "missing": JobState.COMPLETED,
        },
    ).ready


def test_barrier_is_all_complete_and_reports_each_member_once():
    decision = barrier_readiness(
        ["a", "b", "a", "missing"],
        {"a": JobState.COMPLETED, "b": JobState.REVIEW},
    )

    assert not decision.ready
    assert decision.blockers == ("b", "missing")
    assert barrier_readiness(
        ["a", "b"], {"a": JobState.COMPLETED, "b": JobState.COMPLETED}
    ).ready


def test_gang_waits_until_every_members_dependencies_are_complete(job_factory):
    first = job_factory(id="first", gang_id="gang", dependencies=("dep-a",))
    second = job_factory(id="second", gang_id="gang", dependencies=("dep-b",))
    unrelated = job_factory(id="other", gang_id="other", dependencies=("dep-c",))
    jobs = [unrelated, second, first]

    blocked = gang_readiness(
        first,
        jobs,
        {"dep-a": JobState.COMPLETED, "dep-b": JobState.RUNNING},
    )
    assert not blocked.ready
    assert blocked.blockers == ("dep-b",)

    ready_states = {"dep-a": JobState.COMPLETED, "dep-b": JobState.COMPLETED}
    assert gang_readiness(first, jobs, ready_states).ready
    assert gang_readiness(second, reversed(jobs), ready_states).ready
