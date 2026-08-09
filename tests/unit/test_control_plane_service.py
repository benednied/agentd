from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from agentd.domain.enums import JobState, QoSClass, TailAction
from agentd.domain.models import EffortEstimate, Job, QuotaBudget
from agentd.domain.transitions import transition_job
from agentd.scheduling.reconnaissance import ReconnaissanceOutcome
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore


def _advance(store: SQLiteStateStore, job: Job, *states: JobState) -> Job:
    current = job
    for state in states:
        current, event = transition_job(current, state, f"advance to {state}")
        store.save_job(current, event)
    return current


def test_hors_categorie_submission_dispatches_only_bounded_reconnaissance(
    make_job: Callable[..., Job],
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLiteStateStore()
    plane = ControlPlane(
        store,
        id_factory=lambda: "recon-1",
        clock=lambda: now,
    )
    parent = make_job(qos=QoSClass.HORS_CATEGORIE)

    submitted = plane.submit(parent)
    reconnaissance = plane.reconnaissance_for(parent.id)

    assert submitted.state is JobState.PLANNING
    assert len(reconnaissance) == 1
    assert reconnaissance[0].id == "recon-1"
    assert reconnaissance[0].state is JobState.READY
    assert reconnaissance[0].qos is not QoSClass.HORS_CATEGORIE
    assert reconnaissance[0].effort.p99 is not None
    assert reconnaissance[0].quota_budget.maximum == 10
    assert "Do not implement the full objective yet" in reconnaissance[0].objective


def test_hors_categorie_promotion_requires_completed_bounded_outcome(
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    plane = ControlPlane(store, id_factory=lambda: "recon-1")
    parent = plane.submit(make_job(qos=QoSClass.HORS_CATEGORIE))
    outcome = ReconnaissanceOutcome(
        execution_steps=("implement", "review"),
        safe_checkpoint_boundaries=("after schema",),
        effort=EffortEstimate(10, 20, 30),
        quota_budget=QuotaBudget(20, review=5, maximum=25),
    )

    with pytest.raises(ValueError, match="must complete"):
        plane.promote_hors_categorie(parent.id, outcome)

    reconnaissance = plane.reconnaissance_for(parent.id)[0]
    _advance(
        store,
        reconnaissance,
        JobState.ADMITTED,
        JobState.RUNNING,
        JobState.COMPLETED,
    )
    promoted = plane.promote_hors_categorie(parent.id, outcome)

    assert promoted.state is JobState.READY
    assert promoted.qos is QoSClass.NORMAL
    assert promoted.effort == outcome.effort


def test_backlog_degradation_and_tail_queries_are_explicit(
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    plane = ControlPlane(store)
    job = plane.submit(
        make_job(
            allowed_harnesses=("fake", "local"),
            preferred_harnesses=("fake",),
        )
    )

    deferred = plane.return_to_backlog(job.id, reason="capacity throttle")
    degraded = plane.degrade(
        job.id,
        harness="local",
        model_class="economy",
    )
    requeued = plane.requeue(job.id)

    assert deferred.state is JobState.BACKLOG
    assert degraded.preferred_harnesses == ("local", "fake")
    assert degraded.minimum_model_class == "economy"
    assert requeued.state is JobState.READY
    assert plane.evaluate_tail(job.id, 21).action is TailAction.REESTIMATE
