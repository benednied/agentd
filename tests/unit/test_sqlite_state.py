from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import JobState
from agentd.domain.models import Job
from agentd.domain.transitions import initial_transition, transition_job
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore


def test_job_snapshots_and_transition_history_survive_restart(
    tmp_path: object, make_job: Callable[..., Job]
) -> None:
    path = tmp_path / "agentd.sqlite"  # type: ignore[operator]
    job = make_job()
    with SQLiteStateStore(path) as store:
        store.create_job(job, initial_transition(job))
        ready, event = transition_job(job, JobState.READY, "ready")
        store.save_job(ready, event)

    with SQLiteStateStore(path) as reopened:
        assert reopened.get_job(job.id) == ready
        history = reopened.list_transitions(job.id)

    assert [item.to_state for item in history] == [
        JobState.BACKLOG,
        JobState.READY,
    ]
    assert [item.reason for item in history] == ["job submitted", "ready"]


def test_store_rejects_unaudited_state_change(
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    job = make_job()
    store.create_job(job, initial_transition(job))

    with pytest.raises(ConcurrentStateError, match="audit transition"):
        store.save_job(replace(job, state=JobState.READY))

    assert store.get_job(job.id).state == JobState.BACKLOG


def test_non_state_metadata_update_does_not_forge_history(
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    job = make_job()
    store.create_job(job, initial_transition(job))

    store.save_job(replace(job, selected_harness="fake"))

    assert store.get_job(job.id).selected_harness == "fake"
    assert len(store.list_transitions(job.id)) == 1
