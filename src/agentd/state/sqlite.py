"""SQLite execution-state repository.

SQLite stores snapshots as JSON together with queryable lifecycle columns. Job
state changes and their audit events are committed atomically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any

from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunState,
)
from agentd.domain.models import (
    Checkpoint,
    Job,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    RunRecord,
    Serializable,
    StateTransition,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import InvalidStateTransition, can_transition
from agentd.state.base import ConcurrentStateError, EntityNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_transitions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_transitions_job
    ON job_transitions(job_id, sequence);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_job
    ON workspaces(job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_job_leased
    ON workspaces(job_id) WHERE state = 'LEASED';

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_allocations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    node_id TEXT NOT NULL REFERENCES nodes(id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_allocations_job
    ON resource_allocations(job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_allocations_job_active
    ON resource_allocations(job_id) WHERE state = 'ACTIVE';

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, started_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_job_active
    ON runs(job_id)
    WHERE state IN ('STARTING', 'RUNNING', 'DRAINING', 'CHECKPOINTED');

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_job
    ON checkpoints(job_id, created_at);

CREATE TABLE IF NOT EXISTS quota_pools (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_reservations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    pool_id TEXT NOT NULL REFERENCES quota_pools(id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_job
    ON quota_reservations(job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_job_active
    ON quota_reservations(job_id) WHERE state = 'ACTIVE';
"""


def _dump(model: Serializable) -> str:
    return json.dumps(model.to_dict(), sort_keys=True, separators=(",", ":"))


def _load[T](payload: str, factory: Callable[[dict[str, Any]], T]) -> T:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Persisted model payload is not an object")
    return factory(data)


class SQLiteStateStore:
    """A small repository implementation suitable for a local control-plane daemon."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.execute("COMMIT")

    def _rollback(self) -> None:
        self._connection.execute("ROLLBACK")

    def create_job(self, job: Job, transition: StateTransition) -> None:
        if transition.job_id != job.id or transition.from_state is not None:
            raise ValueError("A job's initial transition must start from no state")
        if transition.to_state != job.state:
            raise ValueError("Initial transition does not match the job state")
        with self._lock:
            self._begin()
            try:
                self._connection.execute(
                    "INSERT INTO jobs(id, project, state, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.project,
                        job.state.value,
                        job.created_at.isoformat(),
                        _dump(job),
                    ),
                )
                self._insert_transition(transition)
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def save_job(self, job: Job, transition: StateTransition | None = None) -> None:
        with self._lock:
            self._begin()
            try:
                self._save_job_in_transaction(job, transition)
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def save_job_and_run(
        self,
        job: Job,
        transition: StateTransition,
        run: RunRecord,
    ) -> None:
        """Atomically persist a job transition and its corresponding run snapshot."""

        if run.job_id != job.id:
            raise ValueError("The run must belong to the transitioned job")
        with self._lock:
            self._begin()
            try:
                self._save_job_in_transaction(job, transition)
                self._save_run_in_transaction(run)
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _save_job_in_transaction(
        self,
        job: Job,
        transition: StateTransition | None,
    ) -> None:
        row = self._connection.execute(
            "SELECT state FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Job {job.id} does not exist")
        persisted_state = JobState(row["state"])
        if transition is None:
            if persisted_state != job.state:
                raise ConcurrentStateError(
                    "A state change must include its matching audit transition"
                )
        else:
            if (
                transition.job_id != job.id
                or transition.from_state != persisted_state
                or transition.to_state != job.state
            ):
                raise ConcurrentStateError(
                    "Transition does not match persisted and requested job state"
                )
            if not can_transition(persisted_state, job.state):
                raise InvalidStateTransition(
                    f"Job {job.id} cannot transition from "
                    f"{persisted_state} to {job.state}"
                )
            if not transition.reason.strip():
                raise ValueError("A state transition requires a reason")

        cursor = self._connection.execute(
            "UPDATE jobs SET project = ?, state = ?, payload = ? "
            "WHERE id = ? AND state = ?",
            (
                job.project,
                job.state.value,
                _dump(job),
                job.id,
                persisted_state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentStateError(f"Job {job.id} changed concurrently")
        if transition is not None:
            self._insert_transition(transition)

    def _insert_transition(self, transition: StateTransition) -> None:
        self._connection.execute(
            "INSERT INTO job_transitions("
            "id, job_id, from_state, to_state, reason, occurred_at, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                transition.id,
                transition.job_id,
                transition.from_state.value if transition.from_state else None,
                transition.to_state.value,
                transition.reason,
                transition.occurred_at.isoformat(),
                _dump(transition),
            ),
        )

    def get_job(self, job_id: str) -> Job:
        row = self._one("SELECT payload FROM jobs WHERE id = ?", (job_id,), "Job")
        return _load(row["payload"], Job.from_dict)

    def list_jobs(self, states: frozenset[JobState] | None = None) -> list[Job]:
        with self._lock:
            if states:
                ordered = sorted(state.value for state in states)
                marks = ",".join("?" for _ in ordered)
                rows = self._connection.execute(
                    f"SELECT payload FROM jobs WHERE state IN ({marks}) "
                    "ORDER BY created_at, id",
                    ordered,
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM jobs ORDER BY created_at, id"
                ).fetchall()
        return [_load(row["payload"], Job.from_dict) for row in rows]

    def list_transitions(self, job_id: str) -> list[StateTransition]:
        rows = self._all(
            "SELECT payload FROM job_transitions WHERE job_id = ? ORDER BY sequence",
            (job_id,),
        )
        return [_load(row["payload"], StateTransition.from_dict) for row in rows]

    def save_node(self, node: WorkerNode) -> None:
        self._upsert(
            "nodes",
            node.id,
            ("state", "payload"),
            (node.state.value, _dump(node)),
        )

    def register_node(self, node: WorkerNode) -> WorkerNode:
        """Atomically apply topology metadata without overwriting live usage."""

        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT payload FROM nodes WHERE id = ?", (node.id,)
                ).fetchone()
                registered = (
                    replace(
                        node,
                        allocated=_load(row["payload"], WorkerNode.from_dict).allocated,
                    )
                    if row is not None
                    else node
                )
                self._execute_upsert(
                    "nodes",
                    registered.id,
                    ("state", "payload"),
                    (registered.state.value, _dump(registered)),
                )
                self._commit()
                return registered
            except BaseException:
                self._rollback()
                raise

    def get_node(self, node_id: str) -> WorkerNode:
        row = self._one("SELECT payload FROM nodes WHERE id = ?", (node_id,), "Node")
        return _load(row["payload"], WorkerNode.from_dict)

    def list_nodes(self) -> list[WorkerNode]:
        return [
            _load(row["payload"], WorkerNode.from_dict)
            for row in self._all("SELECT payload FROM nodes ORDER BY id")
        ]

    def save_allocation(self, allocation: ResourceAllocation) -> None:
        self._upsert(
            "resource_allocations",
            allocation.id,
            ("job_id", "node_id", "state", "created_at", "payload"),
            (
                allocation.job_id,
                allocation.node_id,
                allocation.state.value,
                allocation.created_at.isoformat(),
                _dump(allocation),
            ),
            immutable_columns=("job_id", "node_id"),
        )

    def allocate_resources(
        self,
        expected_node: WorkerNode,
        updated_node: WorkerNode,
        allocation: ResourceAllocation,
    ) -> None:
        """Atomically update a node snapshot and create its active allocation."""

        if (
            expected_node.id != updated_node.id
            or allocation.node_id != expected_node.id
        ):
            raise ValueError("Allocation and node identifiers must agree")
        if allocation.state is not AllocationState.ACTIVE:
            raise ValueError("A new resource allocation must be active")
        if updated_node.allocated != expected_node.allocated + allocation.resources:
            raise ValueError("Updated node resources do not match the allocation")

        with self._lock:
            self._begin()
            try:
                if (
                    self._connection.execute(
                        "SELECT 1 FROM jobs WHERE id = ?", (allocation.job_id,)
                    ).fetchone()
                    is None
                ):
                    raise EntityNotFoundError(f"Job {allocation.job_id} does not exist")
                self._expect_node(expected_node)
                self._connection.execute(
                    "UPDATE nodes SET state = ?, payload = ? WHERE id = ?",
                    (
                        updated_node.state.value,
                        _dump(updated_node),
                        expected_node.id,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO resource_allocations("
                    "id, job_id, node_id, state, created_at, payload"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        allocation.id,
                        allocation.job_id,
                        allocation.node_id,
                        allocation.state.value,
                        allocation.created_at.isoformat(),
                        _dump(allocation),
                    ),
                )
                self._commit()
            except sqlite3.IntegrityError as error:
                self._rollback()
                raise ConcurrentStateError(
                    f"Could not create allocation for job {allocation.job_id}"
                ) from error
            except BaseException:
                self._rollback()
                raise

    def release_resources(
        self,
        expected_node: WorkerNode,
        updated_node: WorkerNode,
        expected_allocation: ResourceAllocation,
        released_allocation: ResourceAllocation,
    ) -> None:
        """Atomically release an allocation and return capacity to its node."""

        if (
            expected_node.id != updated_node.id
            or expected_allocation.node_id != expected_node.id
            or released_allocation.id != expected_allocation.id
            or released_allocation.job_id != expected_allocation.job_id
            or released_allocation.node_id != expected_allocation.node_id
            or released_allocation.resources != expected_allocation.resources
        ):
            raise ValueError("Released allocation must preserve its ownership")
        if expected_allocation.state is not AllocationState.ACTIVE:
            raise ValueError("Only an active allocation can be released")
        if released_allocation.state is not AllocationState.RELEASED:
            raise ValueError("Released allocation must have RELEASED state")
        if updated_node.allocated != (
            expected_node.allocated - expected_allocation.resources
        ):
            raise ValueError("Updated node resources do not match the release")

        with self._lock:
            self._begin()
            try:
                self._expect_node(expected_node)
                self._expect_allocation(expected_allocation)
                self._connection.execute(
                    "UPDATE nodes SET state = ?, payload = ? WHERE id = ?",
                    (
                        updated_node.state.value,
                        _dump(updated_node),
                        expected_node.id,
                    ),
                )
                self._connection.execute(
                    "UPDATE resource_allocations SET state = ?, payload = ? "
                    "WHERE id = ?",
                    (
                        released_allocation.state.value,
                        _dump(released_allocation),
                        expected_allocation.id,
                    ),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def get_allocation(self, allocation_id: str) -> ResourceAllocation:
        row = self._one(
            "SELECT payload FROM resource_allocations WHERE id = ?",
            (allocation_id,),
            "Resource allocation",
        )
        return _load(row["payload"], ResourceAllocation.from_dict)

    def find_active_allocation(self, job_id: str) -> ResourceAllocation | None:
        rows = self._all(
            "SELECT payload FROM resource_allocations "
            "WHERE job_id = ? AND state = ? ORDER BY created_at DESC LIMIT 1",
            (job_id, AllocationState.ACTIVE.value),
        )
        return _load(rows[0]["payload"], ResourceAllocation.from_dict) if rows else None

    def list_allocations(self, job_id: str | None = None) -> list[ResourceAllocation]:
        query = "SELECT payload FROM resource_allocations"
        args: tuple[str, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            args = (job_id,)
        query += " ORDER BY created_at, id"
        return [
            _load(row["payload"], ResourceAllocation.from_dict)
            for row in self._all(query, args)
        ]

    def save_quota_pool(self, pool: QuotaPool) -> None:
        self._upsert("quota_pools", pool.id, ("payload",), (_dump(pool),))

    def register_quota_pool(self, pool: QuotaPool) -> QuotaPool:
        """Atomically apply pool configuration while preserving live counters."""

        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT payload FROM quota_pools WHERE id = ?", (pool.id,)
                ).fetchone()
                if row is None:
                    registered = pool
                else:
                    current = _load(row["payload"], QuotaPool.from_dict)
                    registered = replace(
                        pool,
                        remaining=current.remaining,
                        reserved=current.reserved,
                        reset_at=current.reset_at,
                        reset_confidence=current.reset_confidence,
                        mode=current.mode,
                    )
                self._execute_upsert(
                    "quota_pools",
                    registered.id,
                    ("payload",),
                    (_dump(registered),),
                )
                self._commit()
                return registered
            except BaseException:
                self._rollback()
                raise

    def update_quota_pool(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
    ) -> None:
        """Compare-and-swap a pool mutation without changing reserved quota."""

        if expected_pool.id != updated_pool.id:
            raise ValueError("Quota pool identifiers must agree")
        if expected_pool.reserved != updated_pool.reserved:
            raise ValueError("A pool mutation cannot change reserved quota")
        with self._lock:
            self._begin()
            try:
                self._expect_quota_pool(expected_pool)
                self._connection.execute(
                    "UPDATE quota_pools SET payload = ? WHERE id = ?",
                    (_dump(updated_pool), expected_pool.id),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def get_quota_pool(self, pool_id: str) -> QuotaPool:
        row = self._one(
            "SELECT payload FROM quota_pools WHERE id = ?", (pool_id,), "Quota pool"
        )
        return _load(row["payload"], QuotaPool.from_dict)

    def list_quota_pools(self) -> list[QuotaPool]:
        return [
            _load(row["payload"], QuotaPool.from_dict)
            for row in self._all("SELECT payload FROM quota_pools ORDER BY id")
        ]

    def save_reservation(self, reservation: QuotaReservation) -> None:
        self._upsert(
            "quota_reservations",
            reservation.id,
            ("job_id", "pool_id", "state", "created_at", "payload"),
            (
                reservation.job_id,
                reservation.pool_id,
                reservation.state.value,
                reservation.created_at.isoformat(),
                _dump(reservation),
            ),
            immutable_columns=("job_id", "pool_id"),
        )

    def reserve_quota(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
        reservation: QuotaReservation,
    ) -> None:
        """Atomically update a quota pool and create its active reservation."""

        if (
            expected_pool.id != updated_pool.id
            or reservation.pool_id != expected_pool.id
        ):
            raise ValueError("Reservation and pool identifiers must agree")
        if reservation.state is not ReservationState.ACTIVE:
            raise ValueError("A new quota reservation must be active")
        if updated_pool.reserved != expected_pool.reserved + reservation.amount:
            raise ValueError("Updated pool quota does not match the reservation")

        with self._lock:
            self._begin()
            try:
                if (
                    self._connection.execute(
                        "SELECT 1 FROM jobs WHERE id = ?", (reservation.job_id,)
                    ).fetchone()
                    is None
                ):
                    raise EntityNotFoundError(
                        f"Job {reservation.job_id} does not exist"
                    )
                self._expect_quota_pool(expected_pool)
                self._connection.execute(
                    "UPDATE quota_pools SET payload = ? WHERE id = ?",
                    (_dump(updated_pool), expected_pool.id),
                )
                self._connection.execute(
                    "INSERT INTO quota_reservations("
                    "id, job_id, pool_id, state, created_at, payload"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        reservation.id,
                        reservation.job_id,
                        reservation.pool_id,
                        reservation.state.value,
                        reservation.created_at.isoformat(),
                        _dump(reservation),
                    ),
                )
                self._commit()
            except sqlite3.IntegrityError as error:
                self._rollback()
                raise ConcurrentStateError(
                    f"Could not reserve quota for job {reservation.job_id}"
                ) from error
            except BaseException:
                self._rollback()
                raise

    def release_quota(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
        expected_reservation: QuotaReservation,
        released_reservation: QuotaReservation,
    ) -> None:
        """Atomically release a reservation and charge its full consumption."""

        if (
            expected_pool.id != updated_pool.id
            or expected_reservation.pool_id != expected_pool.id
            or released_reservation.id != expected_reservation.id
            or released_reservation.job_id != expected_reservation.job_id
            or released_reservation.pool_id != expected_reservation.pool_id
            or released_reservation.amount != expected_reservation.amount
        ):
            raise ValueError("Released reservation must preserve its ownership")
        if expected_reservation.state is not ReservationState.ACTIVE:
            raise ValueError("Only an active reservation can be released")
        if released_reservation.state is ReservationState.ACTIVE:
            raise ValueError("Released reservation cannot remain active")
        if updated_pool.reserved != max(
            0, expected_pool.reserved - expected_reservation.amount
        ):
            raise ValueError("Updated pool quota does not match the release")

        with self._lock:
            self._begin()
            try:
                self._expect_quota_pool(expected_pool)
                self._expect_reservation(expected_reservation)
                self._connection.execute(
                    "UPDATE quota_pools SET payload = ? WHERE id = ?",
                    (_dump(updated_pool), expected_pool.id),
                )
                self._connection.execute(
                    "UPDATE quota_reservations SET state = ?, payload = ? WHERE id = ?",
                    (
                        released_reservation.state.value,
                        _dump(released_reservation),
                        expected_reservation.id,
                    ),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def get_reservation(self, reservation_id: str) -> QuotaReservation:
        row = self._one(
            "SELECT payload FROM quota_reservations WHERE id = ?",
            (reservation_id,),
            "Quota reservation",
        )
        return _load(row["payload"], QuotaReservation.from_dict)

    def find_active_reservation(self, job_id: str) -> QuotaReservation | None:
        rows = self._all(
            "SELECT payload FROM quota_reservations "
            "WHERE job_id = ? AND state = ? ORDER BY created_at DESC LIMIT 1",
            (job_id, ReservationState.ACTIVE.value),
        )
        return _load(rows[0]["payload"], QuotaReservation.from_dict) if rows else None

    def list_reservations(self, job_id: str | None = None) -> list[QuotaReservation]:
        query = "SELECT payload FROM quota_reservations"
        args: tuple[str, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            args = (job_id,)
        query += " ORDER BY created_at, id"
        return [
            _load(row["payload"], QuotaReservation.from_dict)
            for row in self._all(query, args)
        ]

    def save_workspace(self, workspace: WorkspaceLease) -> None:
        self._upsert(
            "workspaces",
            workspace.id,
            ("job_id", "state", "created_at", "payload"),
            (
                workspace.job_id,
                workspace.state.value,
                workspace.created_at.isoformat(),
                _dump(workspace),
            ),
            immutable_columns=("job_id",),
        )

    def get_workspace(self, workspace_id: str) -> WorkspaceLease:
        row = self._one(
            "SELECT payload FROM workspaces WHERE id = ?",
            (workspace_id,),
            "Workspace",
        )
        return _load(row["payload"], WorkspaceLease.from_dict)

    def find_workspace(self, job_id: str) -> WorkspaceLease | None:
        rows = self._all(
            "SELECT payload FROM workspaces WHERE job_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        )
        return _load(rows[0]["payload"], WorkspaceLease.from_dict) if rows else None

    def list_workspaces(self, job_id: str | None = None) -> list[WorkspaceLease]:
        query = "SELECT payload FROM workspaces"
        args: tuple[str, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            args = (job_id,)
        query += " ORDER BY created_at, id"
        return [
            _load(row["payload"], WorkspaceLease.from_dict)
            for row in self._all(query, args)
        ]

    def save_run(self, run: RunRecord) -> None:
        with self._lock:
            self._begin()
            try:
                self._save_run_in_transaction(run)
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _save_run_in_transaction(self, run: RunRecord) -> None:
        self._validate_run_links(run)
        self._execute_upsert(
            "runs",
            run.id,
            ("job_id", "state", "started_at", "payload"),
            (
                run.job_id,
                run.state.value,
                run.started_at.isoformat(),
                _dump(run),
            ),
            immutable_columns=("job_id",),
        )

    def _validate_run_links(self, run: RunRecord) -> None:
        if run.contract.job_id != run.job_id:
            raise ValueError(f"Run {run.id} has a contract for another job")
        if run.handle.driver != run.driver:
            raise ValueError(f"Run {run.id} has a handle for another driver")

        workspace = self._connection.execute(
            "SELECT job_id FROM workspaces WHERE id = ?", (run.workspace_id,)
        ).fetchone()
        if workspace is None:
            raise EntityNotFoundError(
                f"Workspace {run.workspace_id} does not exist for run {run.id}"
            )
        if workspace["job_id"] != run.job_id:
            raise ValueError(f"Run {run.id} references another job's workspace")

        allocation = self._connection.execute(
            "SELECT job_id, node_id FROM resource_allocations WHERE id = ?",
            (run.allocation_id,),
        ).fetchone()
        if allocation is None:
            raise EntityNotFoundError(
                f"Resource allocation {run.allocation_id} does not exist "
                f"for run {run.id}"
            )
        if allocation["job_id"] != run.job_id:
            raise ValueError(f"Run {run.id} references another job's allocation")
        if allocation["node_id"] != run.node_id:
            raise ValueError(f"Run {run.id} allocation belongs to another node")

        reservation = self._connection.execute(
            "SELECT job_id FROM quota_reservations WHERE id = ?",
            (run.reservation_id,),
        ).fetchone()
        if reservation is None:
            raise EntityNotFoundError(
                f"Quota reservation {run.reservation_id} does not exist "
                f"for run {run.id}"
            )
        if reservation["job_id"] != run.job_id:
            raise ValueError(f"Run {run.id} references another job's reservation")

    def get_run(self, run_id: str) -> RunRecord:
        row = self._one("SELECT payload FROM runs WHERE id = ?", (run_id,), "Run")
        return _load(row["payload"], RunRecord.from_dict)

    def find_active_run(self, job_id: str) -> RunRecord | None:
        active = sorted(
            state.value
            for state in {
                RunState.STARTING,
                RunState.RUNNING,
                RunState.DRAINING,
                RunState.CHECKPOINTED,
            }
        )
        marks = ",".join("?" for _ in active)
        rows = self._all(
            f"SELECT payload FROM runs WHERE job_id = ? "
            f"AND state IN ({marks}) ORDER BY started_at DESC, id DESC LIMIT 1",
            (job_id, *active),
        )
        return _load(rows[0]["payload"], RunRecord.from_dict) if rows else None

    def latest_run(self, job_id: str) -> RunRecord | None:
        rows = self._all(
            "SELECT payload FROM runs WHERE job_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (job_id,),
        )
        return _load(rows[0]["payload"], RunRecord.from_dict) if rows else None

    def list_runs(self, job_id: str | None = None) -> list[RunRecord]:
        query = "SELECT payload FROM runs"
        args: tuple[str, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            args = (job_id,)
        query += " ORDER BY started_at, id"
        return [
            _load(row["payload"], RunRecord.from_dict) for row in self._all(query, args)
        ]

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self._lock:
            self._begin()
            try:
                run = self._connection.execute(
                    "SELECT job_id FROM runs WHERE id = ?", (checkpoint.run_id,)
                ).fetchone()
                if run is None:
                    raise EntityNotFoundError(
                        f"Run {checkpoint.run_id} does not exist for checkpoint "
                        f"{checkpoint.id}"
                    )
                if run["job_id"] != checkpoint.job_id:
                    raise ValueError(
                        f"Checkpoint {checkpoint.id} belongs to another run's job"
                    )
                self._execute_upsert(
                    "checkpoints",
                    checkpoint.id,
                    ("job_id", "run_id", "created_at", "payload"),
                    (
                        checkpoint.job_id,
                        checkpoint.run_id,
                        checkpoint.created_at.isoformat(),
                        _dump(checkpoint),
                    ),
                    immutable_columns=("job_id", "run_id"),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def latest_checkpoint(self, job_id: str) -> Checkpoint | None:
        rows = self._all(
            "SELECT payload FROM checkpoints WHERE job_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (job_id,),
        )
        return _load(rows[0]["payload"], Checkpoint.from_dict) if rows else None

    def list_checkpoints(self, job_id: str) -> list[Checkpoint]:
        return [
            _load(row["payload"], Checkpoint.from_dict)
            for row in self._all(
                "SELECT payload FROM checkpoints WHERE job_id = ? "
                "ORDER BY created_at, id",
                (job_id,),
            )
        ]

    def _upsert(
        self,
        table: str,
        entity_id: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        *,
        immutable_columns: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            self._execute_upsert(
                table,
                entity_id,
                columns,
                values,
                immutable_columns=immutable_columns,
            )

    def _execute_upsert(
        self,
        table: str,
        entity_id: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        *,
        immutable_columns: tuple[str, ...] = (),
    ) -> None:
        names = ("id", *columns)
        marks = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
        query = (
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        if immutable_columns:
            ownership = " AND ".join(
                f"{table}.{name} = excluded.{name}" for name in immutable_columns
            )
            query += f" WHERE {ownership}"
        cursor = self._connection.execute(query, (entity_id, *values))
        if cursor.rowcount != 1:
            raise ValueError(f"{table} {entity_id} cannot change ownership")

    def _expect_node(self, expected: WorkerNode) -> None:
        row = self._connection.execute(
            "SELECT payload FROM nodes WHERE id = ?", (expected.id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Node {expected.id} does not exist")
        if _load(row["payload"], WorkerNode.from_dict) != expected:
            raise ConcurrentStateError(f"Node {expected.id} changed concurrently")

    def _expect_allocation(self, expected: ResourceAllocation) -> None:
        row = self._connection.execute(
            "SELECT payload FROM resource_allocations WHERE id = ?", (expected.id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(
                f"Resource allocation {expected.id} does not exist"
            )
        if _load(row["payload"], ResourceAllocation.from_dict) != expected:
            raise ConcurrentStateError(f"Allocation {expected.id} changed concurrently")

    def _expect_quota_pool(self, expected: QuotaPool) -> None:
        row = self._connection.execute(
            "SELECT payload FROM quota_pools WHERE id = ?", (expected.id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Quota pool {expected.id} does not exist")
        if _load(row["payload"], QuotaPool.from_dict) != expected:
            raise ConcurrentStateError(f"Quota pool {expected.id} changed concurrently")

    def _expect_reservation(self, expected: QuotaReservation) -> None:
        row = self._connection.execute(
            "SELECT payload FROM quota_reservations WHERE id = ?", (expected.id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Quota reservation {expected.id} does not exist")
        if _load(row["payload"], QuotaReservation.from_dict) != expected:
            raise ConcurrentStateError(
                f"Reservation {expected.id} changed concurrently"
            )

    def _one(
        self, query: str, args: tuple[object, ...], entity_name: str
    ) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(query, args).fetchone()
        if row is None:
            identifier = args[0] if args else "unknown"
            raise EntityNotFoundError(f"{entity_name} {identifier} does not exist")
        return row

    def _all(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(query, args).fetchall())
