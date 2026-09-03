from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentd.domain.enums import AgentRequestKind, ArtifactKind, JobState, RunState
from agentd.domain.models import (
    AgentRequestRecord,
    ArtifactRecord,
    ArtifactRef,
    ExecutionContract,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    ResourceVector,
    RunHandle,
    RunRecord,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import initial_transition, transition_job
from agentd.state.base import ConcurrentStateError, EntityNotFoundError
from agentd.state.sqlite import (
    SCHEMA,
    SCHEMA_VERSION,
    SQLiteStateStore,
    _migrate_v1_to_v2,
    _migrate_v2_to_v3,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
GIT_SHA = "a" * 40


def _seed_run(
    make_job,
    store: SQLiteStateStore,
    *,
    job_id: str = "artifact-job",
    run_id: str = "artifact-run",
) -> tuple[object, RunRecord]:
    job = make_job(id=job_id, state=JobState.RUNNING)
    store.create_job(job, initial_transition(job, reason="test job"))
    contract = ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="repository",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=("/workspace",),
        checkpoint_expectations="safe boundaries",
        coordination_mechanisms=(),
        completion_protocol="report",
        working_directory="/workspace",
        environment={},
        model_class="standard",
    )
    run = RunRecord(
        id=run_id,
        job_id=job.id,
        node_id=f"{run_id}-node",
        workspace_id=f"{run_id}-workspace",
        reservation_id=f"{run_id}-reservation",
        allocation_id=f"{run_id}-allocation",
        driver="fake",
        backend="direct",
        contract=contract,
        handle=RunHandle(id="artifact-handle", driver="fake"),
        state=RunState.RUNNING,
        started_at=NOW,
    )
    store.save_node(
        WorkerNode(
            id=run.node_id,
            labels={},
            capacity=ResourceVector(cpu=4, ram_gb=8),
            allocated=job.resources,
            harnesses=frozenset({"fake"}),
            updated_at=NOW,
        )
    )
    store.save_quota_pool(
        QuotaPool(
            id=job.quota_budget.pool_id,
            provider="test",
            remaining=100,
            reserved=job.quota_budget.expected_path,
            updated_at=NOW,
        )
    )
    store.save_allocation(
        ResourceAllocation(
            id=run.allocation_id,
            job_id=job.id,
            node_id=run.node_id,
            resources=job.resources,
            created_at=NOW,
        )
    )
    store.save_reservation(
        QuotaReservation(
            id=run.reservation_id,
            job_id=job.id,
            pool_id=job.quota_budget.pool_id,
            amount=job.quota_budget.expected_path,
            created_at=NOW,
        )
    )
    store.save_workspace(
        WorkspaceLease(
            id=run.workspace_id,
            job_id=job.id,
            repository=job.repository,
            branch="artifact-branch",
            working_directory=contract.working_directory,
            base_ref="HEAD",
            created_at=NOW,
        ),
        expected=None,
    )
    store.save_run(run, expected=None)
    return job, run


def _record(run: RunRecord, *, artifact_id: str = "artifact-1") -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        ref=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
        producer_job_id=run.job_id,
        producer_run_id=run.id,
        spec_name="source",
        created_at=NOW,
    )


def test_artifact_ledger_is_idempotent_and_rejects_conflicts(make_job) -> None:
    store = SQLiteStateStore()
    job, run = _seed_run(make_job, store)
    artifact = _record(run)

    assert store.publish_artifact(artifact) == artifact
    assert store.publish_artifact(artifact) == artifact
    assert store.get_artifact(artifact.id) == artifact
    assert store.list_artifacts(job_id=job.id) == [artifact]
    assert store.list_artifacts(run_id=run.id) == [artifact]

    changed = ArtifactRecord(
        id=artifact.id,
        ref=artifact.ref,
        producer_job_id=artifact.producer_job_id,
        producer_run_id=artifact.producer_run_id,
        spec_name="different",
        created_at=NOW,
    )
    with pytest.raises(ConcurrentStateError, match="already differs"):
        store.publish_artifact(changed)
    different_id = ArtifactRecord(
        id="artifact-2",
        ref=artifact.ref,
        producer_job_id=artifact.producer_job_id,
        producer_run_id=artifact.producer_run_id,
        spec_name=artifact.spec_name,
        created_at=NOW,
    )
    with pytest.raises(ConcurrentStateError, match="already published"):
        store.publish_artifact(different_id)
    store.close()


def test_same_immutable_ref_can_be_published_by_different_producers(make_job) -> None:
    store = SQLiteStateStore()
    first_job, first_run = _seed_run(make_job, store)
    second_job, second_run = _seed_run(
        make_job,
        store,
        job_id="artifact-job-2",
        run_id="artifact-run-2",
    )
    first = _record(first_run, artifact_id="artifact-first")
    second = _record(second_run, artifact_id="artifact-second")

    assert store.publish_artifact(first) == first
    assert store.publish_artifact(second) == second
    assert store.list_artifacts() == [first, second]
    assert store.list_artifacts(job_id=first_job.id) == [first]
    assert store.list_artifacts(job_id=second_job.id) == [second]
    store.close()


def test_artifact_slot_conflict_rolls_back_the_whole_batch(make_job) -> None:
    store = SQLiteStateStore()
    _job, run = _seed_run(make_job, store)
    existing = _record(run, artifact_id="artifact-existing")
    store.publish_artifact(existing)
    prior_slot = replace(existing, id="artifact-prior", spec_name="other")
    conflicting = replace(existing, id="artifact-conflicting")

    with pytest.raises(ConcurrentStateError, match="producer slot"):
        store.publish_artifacts((prior_slot, conflicting))

    assert store.list_artifacts() == [existing]
    store.close()


def test_external_artifact_registration_has_no_fake_foreign_keys(tmp_path) -> None:
    path = tmp_path / "external.sqlite"
    store = SQLiteStateStore(path)
    external = ArtifactRecord(
        id="external-image",
        ref=ArtifactRef(
            ArtifactKind.OCI_IMAGE,
            "ghcr.io/acme/app@sha256:" + "b" * 64,
        ),
        producer_job_id=None,
        producer_run_id=None,
        spec_name="base-image",
        verified=True,
        verified_at=NOW,
        external=True,
    )
    assert store.register_external_artifact(external) == external
    assert store.register_external_artifact(external) == external
    with pytest.raises(ValueError, match="register_external_artifact"):
        store.publish_artifact(external)
    store.close()
    reopened = SQLiteStateStore(path)
    assert reopened.get_artifact(external.id) == external
    reopened.close()


def test_atomic_job_run_and_artifacts_roll_back_on_late_conflict(make_job) -> None:
    store = SQLiteStateStore()
    job, run = _seed_run(make_job, store)
    completed_job, transition = transition_job(
        job, JobState.COMPLETED, "artifact output accepted", at=NOW
    )
    completed_run = replace(run, state=RunState.COMPLETED)
    valid = _record(run, artifact_id="artifact-valid")
    invalid = ArtifactRecord(
        id="artifact-invalid",
        ref=ArtifactRef(ArtifactKind.GIT_COMMIT, "c" * 40),
        producer_job_id=job.id,
        producer_run_id="missing-run",
        spec_name="source",
        created_at=NOW,
    )

    with pytest.raises(EntityNotFoundError, match="missing-run"):
        store.save_job_and_run_with_artifacts(
            completed_job,
            transition,
            completed_run,
            (valid, invalid),
            expected_job=job,
            expected_run=run,
        )
    assert store.get_job(job.id) == job
    assert store.get_run(run.id) == run
    assert store.list_artifacts() == []
    store.close()


def test_v1_migration_is_atomic_bootstrap_and_idempotent(tmp_path) -> None:
    path = tmp_path / "migration.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA user_version = 1")
    connection.execute("CREATE TABLE legacy_marker (value TEXT)")
    connection.commit()
    connection.close()

    store = SQLiteStateStore(path)
    assert store.schema_version == SCHEMA_VERSION
    tables = {
        row[0]
        for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"artifacts", "agent_messages", "legacy_marker"} <= tables
    store.close()

    reopened = SQLiteStateStore(path)
    assert reopened.schema_version == SCHEMA_VERSION
    reopened.close()


def test_v3_migration_drops_digest_unique_and_preserves_artifacts(tmp_path) -> None:
    path = tmp_path / "v3-artifacts.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        _migrate_v1_to_v2(connection)
        _migrate_v2_to_v3(connection)
        connection.execute(
            """
            INSERT INTO artifacts(
                id, kind, value, producer_job_id, producer_run_id, spec_name,
                verified, verified_at, created_at, payload, external
            ) VALUES (?, ?, ?, NULL, NULL, ?, 1, ?, ?, ?, 1)
            """,
            (
                "legacy-artifact",
                ArtifactKind.GIT_COMMIT.value,
                GIT_SHA,
                "legacy-source",
                NOW.isoformat(),
                NOW.isoformat(),
                "legacy-payload",
            ),
        )
        connection.execute("PRAGMA user_version = 3")

    store = SQLiteStateStore(path)
    assert store.schema_version == SCHEMA_VERSION == 4
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE id = 'legacy-artifact'"
        ).fetchone()[0]
        == 1
    )
    unique_columns = []
    indexed_columns = []
    for index in store._connection.execute("PRAGMA index_list('artifacts')"):
        name = str(index[1]).replace('"', '""')
        columns = tuple(
            str(column[2])
            for column in store._connection.execute(f'PRAGMA index_info("{name}")')
        )
        indexed_columns.append(columns)
        if bool(index[2]):
            unique_columns.append(columns)
    assert ("kind", "value") not in unique_columns
    assert ("producer_job_id", "spec_name") not in unique_columns
    assert ("producer_job_id", "spec_name") in indexed_columns
    trigger = store._connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'prevent_artifact_producer_slot_conflict'"
    ).fetchone()
    assert trigger is not None
    store.close()


def test_v3_migration_preserves_legacy_duplicate_producer_slots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3-duplicate-slots.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        _migrate_v1_to_v2(connection)
        _migrate_v2_to_v3(connection)
        connection.execute(
            "INSERT INTO jobs(id, project, state, created_at, payload) "
            "VALUES ('job', 'project', 'COMPLETED', ?, '{}')",
            (NOW.isoformat(),),
        )
        connection.execute(
            "INSERT INTO runs(id, job_id, state, started_at, payload) "
            "VALUES ('run', 'job', 'COMPLETED', ?, '{}')",
            (NOW.isoformat(),),
        )
        for artifact_id, value in (
            ("legacy-first", GIT_SHA),
            ("legacy-second", "b" * 40),
        ):
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, kind, value, producer_job_id, producer_run_id,
                    spec_name, verified, verified_at, created_at, payload,
                    external
                ) VALUES (?, 'git_commit', ?, 'job', 'run', 'source',
                          0, NULL, ?, '{}', 0)
                """,
                (artifact_id, value, NOW.isoformat()),
            )
        connection.execute("PRAGMA user_version = 3")

    store = SQLiteStateStore(path)
    assert store.schema_version == SCHEMA_VERSION
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE producer_job_id = 'job' "
            "AND spec_name = 'source'"
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(sqlite3.IntegrityError, match="producer slot"):
        store._connection.execute(
            """
            INSERT INTO artifacts(
                id, kind, value, producer_job_id, producer_run_id,
                spec_name, verified, verified_at, created_at, payload, external
            ) VALUES ('new-conflict', 'git_commit', ?, 'job', 'run', 'source',
                      0, NULL, ?, '{}', 0)
            """,
            ("c" * 40, NOW.isoformat()),
        )
    store.close()


def test_durable_agent_messages_replay_in_sequence(make_job, tmp_path) -> None:
    path = tmp_path / "messages.sqlite"
    store = SQLiteStateStore(path)
    job, run = _seed_run(make_job, store)
    first = AgentRequestRecord(
        request_id="request-1",
        sequence=0,
        run_id=run.id,
        kind=AgentRequestKind.REFINEMENT,
        message="Need clarification",
        created_at=NOW,
        job_id=job.id,
    )
    persisted = store.append_agent_request(first)
    assert persisted.sequence == 1
    assert store.append_agent_request(first) == persisted
    store.close()

    reopened = SQLiteStateStore(path)
    assert reopened.list_agent_requests(run.id, limit=1) == [persisted]
    assert reopened.list_agent_requests(run.id, limit=0) == []
    reopened.close()
