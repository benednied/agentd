"""SQLite execution-state repository.

SQLite stores snapshots as JSON together with queryable lifecycle columns. Job
state changes and their audit events are committed atomically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from math import isfinite
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
    DriverSession,
    Job,
    ProviderQuotaSnapshot,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    RunCommand,
    RunCommandAck,
    RunObservation,
    RunRecord,
    Serializable,
    StateTransition,
    UsageApplication,
    UsageSample,
    WorkerNode,
    WorkspaceLease,
    utc_now,
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_job_outstanding
    ON quota_reservations(job_id)
    WHERE state IN ('ACTIVE', 'METERING_PENDING');

CREATE TABLE IF NOT EXISTS usage_samples (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    reservation_id TEXT NOT NULL REFERENCES quota_reservations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    source TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    cumulative_quota REAL NOT NULL,
    delta REAL NOT NULL,
    observed_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(run_id, thread_id, turn_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_usage_samples_run
    ON usage_samples(run_id, observed_at, sequence);
CREATE INDEX IF NOT EXISTS idx_usage_samples_job
    ON usage_samples(job_id, observed_at);

CREATE TABLE IF NOT EXISTS driver_sessions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    driver TEXT NOT NULL,
    observation_cursor TEXT,
    active INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_quota_snapshots (
    id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES quota_pools(id),
    bucket_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_snapshots_pool
    ON provider_quota_snapshots(pool_id, observed_at, id);

CREATE TABLE IF NOT EXISTS run_commands (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_commands_pending
    ON run_commands(run_id, created_at, id);

CREATE TABLE IF NOT EXISTS run_command_acks (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE REFERENCES run_commands(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    acknowledged_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

SCHEMA_VERSION = 1


def _dump(model: Serializable) -> str:
    return json.dumps(model.to_dict(), sort_keys=True, separators=(",", ":"))


def _load[T](payload: str, factory: Callable[[dict[str, Any]], T]) -> T:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Persisted model payload is not an object")
    return factory(data)


class SQLiteStateStore:
    """A small repository implementation suitable for a local control-plane daemon."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = str(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        try:
            self.initialize()
        except BaseException:
            self._connection.close()
            raise

    def initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            row = self._connection.execute("PRAGMA user_version").fetchone()
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == 0:
                self._connection.executescript(SCHEMA)
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        """Return the version of the durable schema opened by this store."""

        with self._lock:
            return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def busy_timeout_ms(self) -> int:
        """Return this connection's lock-contention wait in milliseconds."""

        with self._lock:
            return int(self._connection.execute("PRAGMA busy_timeout").fetchone()[0])

    def __enter__(self) -> SQLiteStateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Commit one immediate transaction or reliably roll it back."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

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
        with self._lock, self._transaction():
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

    def save_job(self, job: Job, transition: StateTransition | None = None) -> None:
        with self._lock, self._transaction():
            self._save_job_in_transaction(job, transition)

    def save_job_and_run(
        self,
        job: Job,
        transition: StateTransition,
        run: RunRecord,
    ) -> None:
        """Atomically persist a job transition and its corresponding run snapshot."""

        if run.job_id != job.id:
            raise ValueError("The run must belong to the transitioned job")
        with self._lock, self._transaction():
            self._save_job_in_transaction(job, transition)
            self._save_run_in_transaction(run)

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

        with self._lock, self._transaction():
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
            return registered

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

        with self._lock, self._transaction():
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
                "UPDATE resource_allocations SET state = ?, payload = ? WHERE id = ?",
                (
                    released_allocation.state.value,
                    _dump(released_allocation),
                    expected_allocation.id,
                ),
            )

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

        with self._lock, self._transaction():
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
                    debt=current.debt,
                    unit=current.unit,
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
            return registered

    def update_quota_pool(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
    ) -> None:
        """Compare-and-swap a pool mutation without changing reserved quota."""

        if expected_pool.id != updated_pool.id:
            raise ValueError("Quota pool identifiers must agree")
        if expected_pool.unit is not updated_pool.unit:
            raise ValueError("A quota pool mutation cannot change units")
        if expected_pool.reserved != updated_pool.reserved:
            raise ValueError("A pool mutation cannot change reserved quota")
        with self._lock, self._transaction():
            self._expect_quota_pool(expected_pool)
            self._connection.execute(
                "UPDATE quota_pools SET payload = ? WHERE id = ?",
                (_dump(updated_pool), expected_pool.id),
            )

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
            or expected_pool.unit is not updated_pool.unit
            or reservation.unit is not expected_pool.unit
        ):
            raise ValueError("Reservation and pool identifiers/units must agree")
        if reservation.state is not ReservationState.ACTIVE:
            raise ValueError("A new quota reservation must be active")
        if updated_pool.reserved != expected_pool.reserved + reservation.amount:
            raise ValueError("Updated pool quota does not match the reservation")
        if (
            updated_pool.remaining != expected_pool.remaining
            or updated_pool.debt != expected_pool.debt
        ):
            raise ValueError("Reservation cannot charge or forgive quota")

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
            or released_reservation.unit is not expected_reservation.unit
            or expected_reservation.unit is not expected_pool.unit
            or updated_pool.unit is not expected_pool.unit
        ):
            raise ValueError("Released reservation must preserve its ownership")
        if expected_reservation.state not in {
            ReservationState.ACTIVE,
            ReservationState.METERING_PENDING,
        }:
            raise ValueError("Only an outstanding reservation can be released")
        if released_reservation.state in {
            ReservationState.ACTIVE,
            ReservationState.METERING_PENDING,
        }:
            raise ValueError("Released reservation cannot remain outstanding")
        if updated_pool.reserved != max(
            0, expected_pool.reserved - expected_reservation.outstanding
        ):
            raise ValueError("Updated pool quota does not match the release")
        delta = released_reservation.consumed - expected_reservation.consumed
        if delta < 0:
            raise ValueError("Final consumption cannot move backwards")
        debt_delta = max(0, delta - expected_pool.remaining)
        if (
            updated_pool.remaining != max(0, expected_pool.remaining - delta)
            or updated_pool.debt != expected_pool.debt + debt_delta
            or released_reservation.debt != expected_reservation.debt + debt_delta
        ):
            raise ValueError("Updated pool quota does not match final consumption")

        with self._lock, self._transaction():
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

    def get_reservation(self, reservation_id: str) -> QuotaReservation:
        row = self._one(
            "SELECT payload FROM quota_reservations WHERE id = ?",
            (reservation_id,),
            "Quota reservation",
        )
        return _load(row["payload"], QuotaReservation.from_dict)

    def find_active_reservation(self, job_id: str) -> QuotaReservation | None:
        outstanding = (
            ReservationState.ACTIVE.value,
            ReservationState.METERING_PENDING.value,
        )
        rows = self._all(
            "SELECT payload FROM quota_reservations "
            "WHERE job_id = ? AND state IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (job_id, *outstanding),
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

    def apply_usage_sample(
        self,
        sample: UsageSample,
        *,
        maximum: float | None = None,
    ) -> UsageApplication:
        """Append and charge one cumulative sample in the same transaction.

        Event identity is ``(run, thread, turn, sequence)``. Exact retries return
        the existing application without charging twice; conflicting or
        non-monotonic readings are rejected.
        """

        if maximum is not None and (not isfinite(maximum) or maximum < 0):
            raise ValueError("A usage maximum must be finite and non-negative")
        with self._lock, self._transaction():
            application = self._apply_usage_sample_in_transaction(
                sample,
                maximum=maximum,
            )
            return application

    def _apply_usage_sample_in_transaction(
        self,
        sample: UsageSample,
        *,
        maximum: float | None,
    ) -> UsageApplication:
        run_row = self._connection.execute(
            "SELECT job_id, payload FROM runs WHERE id = ?", (sample.run_id,)
        ).fetchone()
        if run_row is None:
            raise EntityNotFoundError(f"Run {sample.run_id} does not exist")
        run = _load(run_row["payload"], RunRecord.from_dict)
        job_row = self._connection.execute(
            "SELECT state, payload FROM jobs WHERE id = ?", (run.job_id,)
        ).fetchone()
        if job_row is None:
            raise EntityNotFoundError(f"Job {run.job_id} does not exist")
        job = _load(job_row["payload"], Job.from_dict)
        configured_maximum = job.quota_budget.maximum
        if configured_maximum is not None and (
            maximum is None or configured_maximum < maximum
        ):
            maximum = configured_maximum
        duplicate_row = self._connection.execute(
            "SELECT payload FROM usage_samples WHERE run_id = ? AND thread_id = ? "
            "AND turn_id = ? AND sequence = ?",
            (sample.run_id, sample.thread_id, sample.turn_id, sample.sequence),
        ).fetchone()
        if duplicate_row is not None:
            stored = _load(duplicate_row["payload"], UsageSample.from_dict)
            if not self._same_usage_reading(stored, sample):
                raise ConcurrentStateError(
                    "Usage sequence already contains a different reading"
                )
            reservation = self._reservation_for_usage(
                run.reservation_id,
                run.job_id,
            )
            pool = self._pool_for_usage(reservation)
            job_consumed = self._job_consumed(run.job_id)
            return UsageApplication(
                sample=stored,
                delta=0,
                duplicate=True,
                reservation=reservation,
                pool=pool,
                job_consumed=job_consumed,
                maximum=maximum,
                maximum_exceeded=(maximum is not None and job_consumed >= maximum),
            )
        job_state = JobState(job_row["state"])
        if job_state not in {JobState.RUNNING, JobState.METERING_PENDING}:
            raise ValueError(
                f"Job {run.job_id} cannot accept telemetry while {job_state}"
            )

        reservation = self._reservation_for_usage(run.reservation_id, run.job_id)
        if reservation.state not in {
            ReservationState.ACTIVE,
            ReservationState.METERING_PENDING,
        }:
            raise ValueError("Telemetry requires an outstanding quota reservation")
        pool = self._pool_for_usage(reservation)
        if sample.unit is not reservation.unit or sample.unit is not pool.unit:
            raise ValueError("Usage, reservation, and pool quota units must agree")

        previous_row = self._connection.execute(
            "SELECT payload FROM usage_samples WHERE run_id = ? AND thread_id = ? "
            "AND turn_id = ? ORDER BY sequence DESC LIMIT 1",
            (sample.run_id, sample.thread_id, sample.turn_id),
        ).fetchone()
        previous = (
            _load(previous_row["payload"], UsageSample.from_dict)
            if previous_row is not None
            else None
        )
        if previous is not None and sample.sequence <= previous.sequence:
            raise ConcurrentStateError("Usage samples must have increasing sequences")
        previous_cumulative = previous.cumulative_quota if previous is not None else 0
        delta = sample.cumulative_quota - previous_cumulative
        if delta < 0 or (delta == 0 and (not sample.final or previous is None)):
            raise ValueError(
                "A new usage sample must contribute a positive delta, unless it "
                "is a final marker for existing cumulative usage"
            )
        if (
            previous is not None
            and previous.tokens is not None
            and sample.tokens is not None
            and not sample.tokens.dominates(previous.tokens)
        ):
            raise ValueError("Cumulative token counters cannot move backwards")

        outstanding = reservation.outstanding
        reserved_charge = min(delta, outstanding)
        if pool.reserved + 1e-9 < reserved_charge:
            raise ConcurrentStateError(
                f"Pool {pool.id} reserves less than usage reservation {reservation.id}"
            )
        debt_incurred = max(0, delta - pool.remaining)
        applied = replace(sample, delta=delta)
        updated_reservation = replace(
            reservation,
            consumed=reservation.consumed + delta,
            debt=reservation.debt + debt_incurred,
        )
        updated_pool = replace(
            pool,
            remaining=max(0, pool.remaining - delta),
            reserved=max(0, pool.reserved - reserved_charge),
            debt=pool.debt + debt_incurred,
            updated_at=utc_now(),
        )
        self._connection.execute(
            "INSERT INTO usage_samples("
            "id, run_id, reservation_id, job_id, thread_id, turn_id, source, "
            "sequence, cumulative_quota, delta, observed_at, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                applied.id,
                applied.run_id,
                reservation.id,
                reservation.job_id,
                applied.thread_id,
                applied.turn_id,
                applied.source,
                applied.sequence,
                applied.cumulative_quota,
                delta,
                applied.observed_at.isoformat(),
                _dump(applied),
            ),
        )
        self._connection.execute(
            "UPDATE quota_reservations SET payload = ? WHERE id = ?",
            (_dump(updated_reservation), reservation.id),
        )
        self._connection.execute(
            "UPDATE quota_pools SET payload = ? WHERE id = ?",
            (_dump(updated_pool), pool.id),
        )
        job_consumed = self._job_consumed(
            run.job_id,
            replacement=updated_reservation,
        )
        return UsageApplication(
            sample=applied,
            delta=delta,
            duplicate=False,
            reservation=updated_reservation,
            pool=updated_pool,
            job_consumed=job_consumed,
            maximum=maximum,
            maximum_exceeded=maximum is not None and job_consumed >= maximum,
            debt_incurred=debt_incurred,
        )

    def list_usage_samples(self, run_id: str) -> list[UsageSample]:
        return [
            _load(row["payload"], UsageSample.from_dict)
            for row in self._all(
                "SELECT payload FROM usage_samples WHERE run_id = ? "
                "ORDER BY observed_at, thread_id, turn_id, sequence",
                (run_id,),
            )
        ]

    def top_up_quota(
        self,
        reservation_id: str,
        amount: float,
        *,
        minimum_dispatchable: float = 0,
    ) -> QuotaReservation:
        if not isfinite(amount) or amount <= 0:
            raise ValueError("A reservation top-up must be finite and positive")
        if not isfinite(minimum_dispatchable) or minimum_dispatchable < 0:
            raise ValueError(
                "Minimum dispatchable quota must be finite and non-negative"
            )
        with self._lock, self._transaction():
            reservation = self._reservation_for_usage(reservation_id)
            if reservation.state is not ReservationState.ACTIVE:
                raise ValueError("Only an active reservation can be topped up")
            pool = self._pool_for_usage(reservation)
            updated_reservation = replace(
                reservation,
                amount=reservation.amount + amount,
            )
            additional_outstanding = (
                updated_reservation.outstanding - reservation.outstanding
            )
            if pool.dispatchable - additional_outstanding < minimum_dispatchable:
                raise ConcurrentStateError(
                    f"Pool {pool.id} has insufficient dispatchable quota"
                )
            updated_pool = replace(
                pool,
                reserved=pool.reserved + additional_outstanding,
                updated_at=utc_now(),
            )
            self._connection.execute(
                "UPDATE quota_reservations SET payload = ? WHERE id = ?",
                (_dump(updated_reservation), reservation.id),
            )
            self._connection.execute(
                "UPDATE quota_pools SET payload = ? WHERE id = ?",
                (_dump(updated_pool), pool.id),
            )
            return updated_reservation

    def begin_metering(
        self,
        job: Job,
        transition: StateTransition,
        reservation_id: str,
    ) -> QuotaReservation:
        if job.state is not JobState.METERING_PENDING:
            raise ValueError("Metering must transition the job to METERING_PENDING")
        with self._lock, self._transaction():
            reservation = self._reservation_for_usage(reservation_id, job.id)
            if reservation.state is not ReservationState.ACTIVE:
                raise ValueError("Only an active reservation can begin metering")
            self._save_job_in_transaction(job, transition)
            pending = replace(
                reservation,
                state=ReservationState.METERING_PENDING,
            )
            self._connection.execute(
                "UPDATE quota_reservations SET state = ?, payload = ? WHERE id = ?",
                (pending.state.value, _dump(pending), pending.id),
            )
            return pending

    def settle_quota_usage(
        self,
        reservation_id: str,
        *,
        final_sample: UsageSample | None = None,
        cancelled: bool = False,
        maximum: float | None = None,
    ) -> QuotaReservation:
        if maximum is not None and (not isfinite(maximum) or maximum < 0):
            raise ValueError("A usage maximum must be finite and non-negative")
        if final_sample is not None and not final_sample.final:
            raise ValueError("A final settlement sample must be marked final")
        with self._lock, self._transaction():
            reservation = self._reservation_for_usage(reservation_id)
            if reservation.state in {
                ReservationState.RELEASED,
                ReservationState.CANCELLED,
            }:
                return reservation
            if reservation.state is not ReservationState.METERING_PENDING:
                raise ValueError("Final settlement requires METERING_PENDING")
            job_row = self._connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (reservation.job_id,)
            ).fetchone()
            if job_row is None:
                raise EntityNotFoundError(f"Job {reservation.job_id} does not exist")
            if JobState(job_row["state"]) is not JobState.METERING_PENDING:
                raise ValueError("Final telemetry requires a metering-pending job")
            if final_sample is not None:
                run = self._connection.execute(
                    "SELECT payload FROM runs WHERE id = ?", (final_sample.run_id,)
                ).fetchone()
                if run is None:
                    raise EntityNotFoundError(
                        f"Run {final_sample.run_id} does not exist"
                    )
                if (
                    _load(run["payload"], RunRecord.from_dict).reservation_id
                    != reservation.id
                ):
                    raise ValueError(
                        "Final sample belongs to another quota reservation"
                    )
                self._apply_usage_sample_in_transaction(
                    final_sample,
                    maximum=maximum,
                )
                reservation = self._reservation_for_usage(reservation_id)

            pool = self._pool_for_usage(reservation)
            outstanding = reservation.outstanding
            if pool.reserved + 1e-9 < outstanding:
                raise ConcurrentStateError(
                    f"Pool {pool.id} reserves less than reservation {reservation.id}"
                )
            state = (
                ReservationState.CANCELLED if cancelled else ReservationState.RELEASED
            )
            settled = replace(
                reservation,
                state=state,
                released_at=utc_now(),
            )
            updated_pool = replace(
                pool,
                reserved=max(0, pool.reserved - outstanding),
                updated_at=utc_now(),
            )
            self._connection.execute(
                "UPDATE quota_reservations SET state = ?, payload = ? WHERE id = ?",
                (settled.state.value, _dump(settled), settled.id),
            )
            self._connection.execute(
                "UPDATE quota_pools SET payload = ? WHERE id = ?",
                (_dump(updated_pool), pool.id),
            )
            return settled

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
        with self._lock, self._transaction():
            self._save_run_in_transaction(run)

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

    def save_driver_session(self, session: DriverSession) -> None:
        with self._lock, self._transaction():
            run_row = self._connection.execute(
                "SELECT payload FROM runs WHERE id = ?", (session.run_id,)
            ).fetchone()
            if run_row is None:
                raise EntityNotFoundError(f"Run {session.run_id} does not exist")
            run = _load(run_row["payload"], RunRecord.from_dict)
            if run.driver != session.driver:
                raise ValueError("Driver session belongs to another driver")
            existing_row = self._connection.execute(
                "SELECT payload FROM driver_sessions WHERE run_id = ?",
                (session.run_id,),
            ).fetchone()
            registered = session
            if existing_row is not None:
                existing = _load(existing_row["payload"], DriverSession.from_dict)
                if existing.driver != session.driver:
                    raise ValueError("Driver session cannot change ownership")
                registered = replace(
                    session,
                    id=existing.id,
                    created_at=existing.created_at,
                )
            self._execute_upsert(
                "driver_sessions",
                registered.id,
                (
                    "run_id",
                    "driver",
                    "observation_cursor",
                    "active",
                    "updated_at",
                    "payload",
                ),
                (
                    registered.run_id,
                    registered.driver,
                    registered.observation_cursor,
                    int(registered.active),
                    registered.updated_at.isoformat(),
                    _dump(registered),
                ),
                immutable_columns=("run_id", "driver"),
            )

    def get_driver_session(self, run_id: str) -> DriverSession:
        row = self._one(
            "SELECT payload FROM driver_sessions WHERE run_id = ?",
            (run_id,),
            "Driver session for run",
        )
        return _load(row["payload"], DriverSession.from_dict)

    def list_driver_sessions(
        self,
        active: bool | None = None,
    ) -> list[DriverSession]:
        query = "SELECT payload FROM driver_sessions"
        args: tuple[object, ...] = ()
        if active is not None:
            query += " WHERE active = ?"
            args = (int(active),)
        query += " ORDER BY updated_at, id"
        return [
            _load(row["payload"], DriverSession.from_dict)
            for row in self._all(query, args)
        ]

    def update_observation_cursor(
        self,
        run_id: str,
        expected_cursor: str | None,
        cursor: str,
        observation: RunObservation | None = None,
    ) -> DriverSession:
        if not cursor.strip():
            raise ValueError("An observation cursor cannot be empty")
        if observation is not None and (
            observation.run_id != run_id or observation.cursor != cursor
        ):
            raise ValueError("Observation cursor and run identifiers must agree")
        with self._lock, self._transaction():
            row = self._connection.execute(
                "SELECT payload FROM driver_sessions WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise EntityNotFoundError(
                    f"Driver session for run {run_id} does not exist"
                )
            session = _load(row["payload"], DriverSession.from_dict)
            if session.observation_cursor != expected_cursor:
                raise ConcurrentStateError(
                    f"Observation cursor for run {run_id} changed concurrently"
                )
            updated = replace(
                session,
                observation_cursor=cursor,
                thread_id=(
                    observation.thread_id
                    if observation is not None
                    else session.thread_id
                ),
                turn_id=(
                    observation.turn_id if observation is not None else session.turn_id
                ),
                last_observation=observation or session.last_observation,
                active=(
                    not observation.terminal
                    if observation is not None
                    else session.active
                ),
                updated_at=utc_now(),
            )
            self._connection.execute(
                "UPDATE driver_sessions SET observation_cursor = ?, active = ?, "
                "updated_at = ?, payload = ? WHERE run_id = ?",
                (
                    updated.observation_cursor,
                    int(updated.active),
                    updated.updated_at.isoformat(),
                    _dump(updated),
                    run_id,
                ),
            )
            return updated

    def append_provider_quota_snapshot(
        self,
        snapshot: ProviderQuotaSnapshot,
    ) -> None:
        with self._lock, self._transaction():
            if (
                self._connection.execute(
                    "SELECT 1 FROM quota_pools WHERE id = ?", (snapshot.pool_id,)
                ).fetchone()
                is None
            ):
                raise EntityNotFoundError(
                    f"Quota pool {snapshot.pool_id} does not exist"
                )
            existing = self._connection.execute(
                "SELECT payload FROM provider_quota_snapshots WHERE id = ?",
                (snapshot.id,),
            ).fetchone()
            if existing is not None:
                if (
                    _load(existing["payload"], ProviderQuotaSnapshot.from_dict)
                    != snapshot
                ):
                    raise ConcurrentStateError(
                        f"Provider snapshot {snapshot.id} already differs"
                    )
                return
            self._connection.execute(
                "INSERT INTO provider_quota_snapshots("
                "id, pool_id, bucket_id, observed_at, payload"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.pool_id,
                    snapshot.bucket_id,
                    snapshot.observed_at.isoformat(),
                    _dump(snapshot),
                ),
            )

    def latest_provider_quota_snapshot(
        self,
        pool_id: str,
        bucket_id: str | None = None,
    ) -> ProviderQuotaSnapshot | None:
        snapshots = self.list_provider_quota_snapshots(pool_id, bucket_id)
        return snapshots[-1] if snapshots else None

    def list_provider_quota_snapshots(
        self,
        pool_id: str,
        bucket_id: str | None = None,
    ) -> list[ProviderQuotaSnapshot]:
        query = "SELECT payload FROM provider_quota_snapshots WHERE pool_id = ?"
        args: tuple[object, ...] = (pool_id,)
        if bucket_id is not None:
            query += " AND bucket_id = ?"
            args = (pool_id, bucket_id)
        query += " ORDER BY observed_at, id"
        return [
            _load(row["payload"], ProviderQuotaSnapshot.from_dict)
            for row in self._all(query, args)
        ]

    def enqueue_run_command(self, command: RunCommand) -> None:
        with self._lock, self._transaction():
            if (
                self._connection.execute(
                    "SELECT 1 FROM runs WHERE id = ?", (command.run_id,)
                ).fetchone()
                is None
            ):
                raise EntityNotFoundError(f"Run {command.run_id} does not exist")
            existing = self._connection.execute(
                "SELECT payload FROM run_commands WHERE id = ?", (command.id,)
            ).fetchone()
            if existing is not None:
                if not self._same_run_command(
                    _load(existing["payload"], RunCommand.from_dict),
                    command,
                ):
                    raise ConcurrentStateError(
                        f"Run command {command.id} already differs"
                    )
                return
            self._connection.execute(
                "INSERT INTO run_commands(id, run_id, action, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    command.id,
                    command.run_id,
                    command.action,
                    command.created_at.isoformat(),
                    _dump(command),
                ),
            )

    def list_run_commands(self, run_id: str) -> list[RunCommand]:
        return [
            _load(row["payload"], RunCommand.from_dict)
            for row in self._all(
                "SELECT payload FROM run_commands WHERE run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ]

    def list_pending_run_commands(
        self,
        run_id: str | None = None,
    ) -> list[RunCommand]:
        query = (
            "SELECT commands.payload FROM run_commands AS commands "
            "LEFT JOIN run_command_acks AS acks ON acks.command_id = commands.id "
            "WHERE acks.command_id IS NULL"
        )
        args: tuple[object, ...] = ()
        if run_id is not None:
            query += " AND commands.run_id = ?"
            args = (run_id,)
        query += (
            " ORDER BY CASE commands.action "
            "WHEN 'checkpoint' THEN 0 WHEN 'suspend' THEN 1 "
            "WHEN 'steer' THEN 2 WHEN 'repair' THEN 3 "
            "WHEN 'interrupt' THEN 4 WHEN 'cancel' THEN 5 ELSE 6 END, "
            "commands.created_at, commands.id"
        )
        return [
            _load(row["payload"], RunCommand.from_dict)
            for row in self._all(query, args)
        ]

    def acknowledge_run_command(self, acknowledgement: RunCommandAck) -> None:
        with self._lock, self._transaction():
            command_row = self._connection.execute(
                "SELECT run_id FROM run_commands WHERE id = ?",
                (acknowledgement.command_id,),
            ).fetchone()
            if command_row is None:
                raise EntityNotFoundError(
                    f"Run command {acknowledgement.command_id} does not exist"
                )
            if command_row["run_id"] != acknowledgement.run_id:
                raise ValueError("Command acknowledgement belongs to another run")
            existing = self._connection.execute(
                "SELECT payload FROM run_command_acks WHERE command_id = ?",
                (acknowledgement.command_id,),
            ).fetchone()
            if existing is not None:
                if (
                    _load(existing["payload"], RunCommandAck.from_dict)
                    != acknowledgement
                ):
                    raise ConcurrentStateError(
                        "Run command already has a different acknowledgement"
                    )
                return
            self._connection.execute(
                "INSERT INTO run_command_acks("
                "id, command_id, run_id, acknowledged_at, payload"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    acknowledgement.id,
                    acknowledgement.command_id,
                    acknowledgement.run_id,
                    acknowledgement.acknowledged_at.isoformat(),
                    _dump(acknowledgement),
                ),
            )

    def get_run_command_ack(self, command_id: str) -> RunCommandAck | None:
        rows = self._all(
            "SELECT payload FROM run_command_acks WHERE command_id = ?",
            (command_id,),
        )
        return _load(rows[0]["payload"], RunCommandAck.from_dict) if rows else None

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self._lock, self._transaction():
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

    def _reservation_for_usage(
        self,
        reservation_id: str,
        job_id: str | None = None,
    ) -> QuotaReservation:
        row = self._connection.execute(
            "SELECT payload FROM quota_reservations WHERE id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(
                f"Quota reservation {reservation_id} does not exist"
            )
        reservation = _load(row["payload"], QuotaReservation.from_dict)
        if job_id is not None and reservation.job_id != job_id:
            raise ValueError("Quota reservation belongs to another job")
        return reservation

    def _pool_for_usage(self, reservation: QuotaReservation) -> QuotaPool:
        row = self._connection.execute(
            "SELECT payload FROM quota_pools WHERE id = ?",
            (reservation.pool_id,),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(
                f"Quota pool {reservation.pool_id} does not exist"
            )
        pool = _load(row["payload"], QuotaPool.from_dict)
        if pool.unit is not reservation.unit:
            raise ValueError("Quota pool and reservation units do not agree")
        return pool

    @staticmethod
    def _same_usage_reading(stored: UsageSample, incoming: UsageSample) -> bool:
        return (
            stored.run_id == incoming.run_id
            and stored.thread_id == incoming.thread_id
            and stored.turn_id == incoming.turn_id
            and stored.sequence == incoming.sequence
            and stored.cumulative_quota == incoming.cumulative_quota
            and stored.unit is incoming.unit
            and stored.source == incoming.source
            and stored.tokens == incoming.tokens
            and stored.provider_epoch == incoming.provider_epoch
            and stored.final == incoming.final
            and stored.metadata == incoming.metadata
        )

    @staticmethod
    def _same_run_command(stored: RunCommand, incoming: RunCommand) -> bool:
        return (
            stored.id == incoming.id
            and stored.run_id == incoming.run_id
            and stored.action == incoming.action
            and stored.payload == incoming.payload
        )

    def _job_consumed(
        self,
        job_id: str,
        *,
        replacement: QuotaReservation | None = None,
    ) -> float:
        rows = self._connection.execute(
            "SELECT payload FROM quota_reservations WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        total = 0.0
        replaced = False
        for row in rows:
            reservation = _load(row["payload"], QuotaReservation.from_dict)
            if replacement is not None and reservation.id == replacement.id:
                reservation = replacement
                replaced = True
            total += reservation.consumed
        if replacement is not None and not replaced:
            total += replacement.consumed
        return total

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
