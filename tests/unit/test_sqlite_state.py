import sqlite3
from collections.abc import Callable
from dataclasses import replace

import pytest

from agentd.domain.enums import JobState
from agentd.domain.models import Job
from agentd.domain.transitions import initial_transition, transition_job
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SCHEMA, SCHEMA_VERSION, SQLiteStateStore


def test_store_sets_schema_version_and_busy_timeout(tmp_path: object) -> None:
    path = tmp_path / "agentd.sqlite"  # type: ignore[operator]

    with SQLiteStateStore(path, busy_timeout_ms=1_234) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.busy_timeout_ms == 1_234
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )


def test_store_rejects_a_newer_schema(tmp_path: object) -> None:
    path = tmp_path / "future.sqlite"  # type: ignore[operator]
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteStateStore(path)


def test_version_zero_schema_is_migrated_idempotently(tmp_path: object) -> None:
    path = tmp_path / "legacy.sqlite"  # type: ignore[operator]
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('preserved')")

    with SQLiteStateStore(path) as migrated:
        assert migrated.schema_version == SCHEMA_VERSION
    with SQLiteStateStore(path) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM legacy_marker").fetchone() == (
            "preserved",
        )


def test_job_snapshots_and_transition_history_survive_restart(
    tmp_path: object, make_job: Callable[..., Job]
) -> None:
    path = tmp_path / "agentd.sqlite"  # type: ignore[operator]
    job = make_job()
    with SQLiteStateStore(path) as store:
        store.create_job(job, initial_transition(job))
        ready, event = transition_job(job, JobState.READY, "ready")
        store.save_job(ready, event, expected=job)

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
        store.save_job(replace(job, state=JobState.READY), expected=job)

    assert store.get_job(job.id).state == JobState.BACKLOG


def test_non_state_metadata_update_does_not_forge_history(
    make_job: Callable[..., Job],
) -> None:
    store = SQLiteStateStore()
    job = make_job()
    store.create_job(job, initial_transition(job))

    store.save_job(replace(job, selected_harness="fake"), expected=job)

    assert store.get_job(job.id).selected_harness == "fake"
    assert len(store.list_transitions(job.id)) == 1


def test_v5_existing_controller_gains_backlog_tables_without_losing_history(tmp_path):
    database = tmp_path / "legacy.sqlite"
    with SQLiteStateStore(database):
        pass
    with sqlite3.connect(database) as connection:
        for table in (
            "github_backlog_gates",
            "github_backlog_bindings",
            "controller_reports",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 5")
        connection.execute(
            "INSERT INTO github_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("issue", "job", "rev", "rev", "operator", "then", 1, 0, "{}"),
        )
    with SQLiteStateStore(database) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.backlog_binding("job") is None
        assert store.backlog_gate("job") is None
        assert store.changed_report("first", {"state": "waiting"})
        assert store.github_source_for_job("job")["approved_revision"] == "rev"
    with SQLiteStateStore(database) as reopened:
        assert not reopened.changed_report("first", {"state": "waiting"})
        assert reopened.github_source_for_job("job")["approved_by"] == "operator"
