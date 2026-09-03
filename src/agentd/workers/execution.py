"""Typed worker-side dispatch of registered harness drivers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from agentd.domain.models import (
    ExecutionContract,
    RunHandle,
    RunObservation,
    RunResult,
)
from agentd.harness.protocol import HarnessDriver, ManagedHarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.workers.errors import WorkerOperationError, WorkerProtocolError
from agentd.workers.journal import JournalEntry, OperationJournal
from agentd.workers.protocol import validate_status_payload
from agentd.workers.remote_protocol import Envelope, payload_hash


@dataclass(slots=True)
class _RunState:
    run_id: str
    start_fingerprint: str
    driver: HarnessDriver
    handle: RunHandle
    managed: bool
    last_observation: RunObservation | None = None
    collected: RunResult | None = None


@dataclass(slots=True)
class _RequestLockState:
    """One request lock plus the calls that still reference it.

    The reference count is incremented synchronously, before the first await
    in ``execute``.  This lets the last caller remove the lock without a
    window in which a duplicate request could observe a newly-created lock
    while another caller is still waiting on the old one.
    """

    lock: asyncio.Lock
    users: int = 0


@dataclass(frozen=True, slots=True)
class OperationResponse:
    """Safe, serializable result of one typed worker operation."""

    payload: dict[str, Any]
    ok: bool = True
    error: str | None = None
    sequence: int = 0


class ExecutionService:
    """Map protocol actions to a closed set of ``HarnessDriver`` methods."""

    def __init__(
        self,
        drivers: DriverRegistry,
        journal: OperationJournal,
        *,
        max_payload_items: int = 64,
    ) -> None:
        if max_payload_items <= 0:
            raise ValueError("max_payload_items must be positive")
        self._drivers = drivers
        self._journal = journal
        self._max_payload_items = max_payload_items
        self._runs: dict[str, _RunState] = {}
        self._request_locks: dict[tuple[str, str], _RequestLockState] = {}
        self._start_lock = asyncio.Lock()
        # Requests are registered before waiting for ``_start_lock`` so a
        # concurrent STATUS cannot observe the short pre-claim interval as an
        # authoritative unknown run.
        self._pending_starts: dict[str, int] = {}
        # A failed start can leave a driver side effect in an unknown state.
        # Keep its identity as a tombstone so a second request with the same
        # durable run id cannot blindly start another one.
        self._start_attempts: dict[str, str] = {}

    async def execute(self, request: Envelope) -> OperationResponse:
        """Execute or replay one authenticated request.

        A pending journal row from an earlier process is treated as unknown and
        rejected.  Repeating a side effect after a worker crash is never safe
        unless the completed response was durably recorded.
        """

        # Heartbeats are authenticated liveness observations, not operations.
        # They must not consume unbounded journal rows or replay a stale
        # response for a caller that happens to reuse a request id.  They do
        # receive a durable monotone event sequence so consumers can order
        # observations with operation responses.
        if request.action in {"heartbeat", "status"}:
            try:
                payload = await self._dispatch(request)
                ok = True
                error: str | None = None
            except (WorkerOperationError, WorkerProtocolError) as operation_error:
                payload = {}
                ok = False
                error = self._safe_error(operation_error)
            sequence = self._journal.next_sequence()
            return OperationResponse(payload, ok, error, sequence)

        request_key = (request.request_id, request.action)
        # Keep the journal primary key schema-compatible while making the
        # complete operation identity run-scoped. Without this, reusing an
        # explicit request id for an empty-payload action such as ``cancel``
        # or ``collect`` could replay another run's response.
        digest = payload_hash(
            {
                "run_id": request.run_id,
                "payload": request.payload,
            }
        )
        lock_state = self._request_locks.get(request_key)
        if lock_state is None:
            lock_state = _RequestLockState(asyncio.Lock())
            self._request_locks[request_key] = lock_state
        lock_state.users += 1
        existing: JournalEntry | None = None
        try:
            async with lock_state.lock:
                # The lock is deliberately acquired before consulting the
                # journal. A reconnect can deliver the same request while the
                # first attempt is still executing; waiting here lets that
                # retry replay the completed response instead of treating the
                # in-process pending row as an unrecoverable crash.
                existing = self._journal.begin(
                    request_id=request.request_id,
                    action=request.action,
                    payload_hash=digest,
                )
                if existing.status == "completed":
                    return self._replay(existing)
                if not existing.reserved_here:
                    # The timestamp comparison is intentionally not used for
                    # ownership; it only documents that a pending row can
                    # come from a prior process. ``begin`` cannot identify
                    # its owner, so a row seen as pending after the initial
                    # lookup is safe to treat as unknown rather than execute
                    # twice.
                    raise WorkerOperationError("operation outcome is unknown")
                if request.action == "start":
                    self._pending_starts[request.run_id] = (
                        self._pending_starts.get(request.run_id, 0) + 1
                    )
                try:
                    payload = await self._dispatch(request)
                    ok = True
                    error: str | None = None
                except (WorkerOperationError, WorkerProtocolError) as operation_error:
                    payload = {}
                    ok = False
                    error = self._safe_error(operation_error)
                except Exception as operation_error:
                    payload = {}
                    ok = False
                    error = f"worker operation failed: {type(operation_error).__name__}"
                sequence = self._journal.next_sequence()
                stored = {"ok": ok, "payload": payload, "error": error}
                self._journal.complete(
                    request_id=request.request_id,
                    action=request.action,
                    payload_hash=digest,
                    response=stored,
                    sequence=sequence,
                )
                return OperationResponse(payload, ok, error, sequence)
        finally:
            if (
                request.action == "start"
                and existing is not None
                and existing.reserved_here
            ):
                pending = self._pending_starts.get(request.run_id, 0)
                if pending <= 1:
                    self._pending_starts.pop(request.run_id, None)
                else:
                    self._pending_starts[request.run_id] = pending - 1
            # This bookkeeping contains no await, so a new caller cannot
            # interleave between the final decrement and removal. If another
            # duplicate is already waiting, its reference keeps the shared
            # lock alive until it has replayed the durable response.
            lock_state.users -= 1
            if (
                lock_state.users == 0
                and self._request_locks.get(request_key) is lock_state
            ):
                self._request_locks.pop(request_key, None)

    async def _dispatch(self, request: Envelope) -> dict[str, Any]:
        action = request.action
        if action == "heartbeat":
            self._expect_fields(request.payload, set())
            capabilities = self._drivers.capabilities()
            return {
                "node_id": request.node_id,
                "session_epoch": request.session_epoch,
                "drivers": [item.name for item in capabilities],
                "driver_features": {
                    item.name: sorted(item.features) for item in capabilities
                },
                "active_runs": len(
                    {
                        state.handle.id
                        for state in self._runs.values()
                        if state.collected is None
                    }
                ),
            }
        if action == "status":
            self._expect_fields(request.payload, set())
            # STATUS and START share one lock.  This makes a durable claim the
            # authority once the start has crossed the claim boundary, while
            # the pending marker still covers callers waiting for this lock.
            async with self._start_lock:
                return await self._status(request)
        if action == "start":
            # The durable run id is the idempotency boundary for starts. The
            # request journal deduplicates one request id, while this gate
            # prevents two distinct request ids from racing the same run.
            async with self._start_lock:
                return await self._start(request)
        state = self._run(request.run_id)
        if action == "observe":
            return await self._observe(request, state)
        if action == "steer":
            payload = self._expect_fields(request.payload, {"instruction"})
            instruction = payload["instruction"]
            if not isinstance(instruction, str) or not instruction.strip():
                raise WorkerProtocolError("instruction must be a non-empty string")
            await state.driver.steer(state.handle, instruction)
            return {}
        if action == "interrupt":
            self._expect_fields(request.payload, set())
            await state.driver.interrupt(state.handle)
            return {}
        if action == "cancel":
            self._expect_fields(request.payload, set())
            await state.driver.cancel(state.handle)
            return {}
        if action == "collect":
            self._expect_fields(request.payload, set())
            result = state.collected
            if result is None:
                result = await state.driver.collect(state.handle)
                state.collected = result
            return {"result": result.to_dict()}
        raise WorkerProtocolError(f"unknown worker action {action!r}")

    async def _status(self, request: Envelope) -> dict[str, Any]:
        """Read worker status while serialized with durable START claims."""

        state = self._runs.get(request.run_id)
        if state is None:
            # Both markers are installed before any await that could expose
            # this run to STATUS.  The durable claim also covers a worker
            # process restart where no in-memory handle survives.
            if (
                request.run_id in self._start_attempts
                or request.run_id in self._pending_starts
                or self._journal.run_claim_state(run_id=request.run_id)
                in {"claimed", "started"}
            ):
                return {"known": True, "terminal": False, "result": None}
            return {"known": False, "terminal": False, "result": None}
        status_method = getattr(state.driver, "status", None)
        if status_method is None:
            result = state.collected
            return {
                "known": True,
                "terminal": result is not None,
                "result": result.to_dict() if result is not None else None,
            }
        raw_status = status_method(state.handle)
        if isawaitable(raw_status):
            raw_status = await raw_status
        status = validate_status_payload(raw_status)
        raw_result = status.get("result")
        if status["terminal"] and raw_result is not None:
            # A terminal status is a durable observation.  Mirror its
            # validated result into the worker-local state immediately
            # so subsequent heartbeats stop counting the run even before
            # a separate collect request arrives.
            if not isinstance(raw_result, dict):  # defensive; validator
                raise WorkerProtocolError("worker status result is malformed")
            try:
                state.collected = RunResult.from_dict(raw_result)
            except Exception as error:
                raise WorkerProtocolError(
                    "worker status result is malformed"
                ) from error
            status = dict(status)
            status["result"] = state.collected.to_dict()
        return status

    async def _start(self, request: Envelope) -> dict[str, Any]:
        payload = self._expect_fields(
            request.payload,
            {"driver", "contract", "managed"},
        )
        driver_name = payload["driver"]
        contract_data = payload["contract"]
        managed = payload["managed"]
        if not isinstance(driver_name, str) or not driver_name.strip():
            raise WorkerProtocolError("driver must be a non-empty string")
        if not isinstance(contract_data, dict):
            raise WorkerProtocolError("contract must be an object")
        if not isinstance(managed, bool):
            raise WorkerProtocolError("managed must be a boolean")
        if not request.run_id:
            raise WorkerProtocolError("start requires a run_id")
        fingerprint = payload_hash(request.payload)
        existing = self._runs.get(request.run_id)
        if existing is not None:
            # A harness id may also be indexed in ``_runs``. Never let a new
            # durable run id overwrite that alias, even if the driver happens
            # to return the same external handle.
            if existing.run_id != request.run_id:
                raise WorkerOperationError(
                    "durable run id collides with an existing harness handle"
                )
            if existing.start_fingerprint != fingerprint:
                raise WorkerOperationError(
                    "durable run id was already started with a different contract"
                )
            return {
                "handle": existing.handle.to_dict(),
                "managed": existing.managed,
            }
        attempted = self._start_attempts.get(request.run_id)
        if attempted is not None:
            if attempted != fingerprint:
                raise WorkerOperationError(
                    "durable run id has an unresolved start with a different contract"
                )
            raise WorkerOperationError("operation outcome is unknown")
        try:
            contract = ExecutionContract.from_dict(contract_data)
            driver = self._drivers.get(driver_name)
        except Exception as error:
            raise WorkerProtocolError("start contract or driver is invalid") from error
        managed_driver: ManagedHarnessDriver | None = None
        if managed:
            if not isinstance(driver, ManagedHarnessDriver):
                # Reject before claiming the durable run id; this safe validation
                # failure must not create a permanent unresolved tombstone.
                raise WorkerOperationError(
                    f"driver {driver_name!r} does not support managed starts"
                )
            managed_driver = driver
        if not self._journal.claim_run(
            run_id=request.run_id,
            start_hash=fingerprint,
        ):
            # A completed request with the original request id would have
            # replayed before dispatch.  Reaching this branch means another
            # request (possibly from an earlier worker lifetime) claimed the
            # durable run id, but this process has no trustworthy handle to
            # return.  Starting again could duplicate an external side effect.
            raise WorkerOperationError("operation outcome is unknown")
        self._start_attempts[request.run_id] = fingerprint
        try:
            if managed_driver is not None:
                handle = await managed_driver.start_managed(request.run_id, contract)
            else:
                handle = await driver.start(contract)
        except BaseException:
            # The driver may have created an external side effect and then
            # raised before returning a handle. Keep the durable claim
            # unresolved so STATUS cannot authorize a duplicate start.
            self._start_attempts.pop(request.run_id, None)
            raise
        state = _RunState(
            run_id=request.run_id,
            start_fingerprint=fingerprint,
            driver=driver,
            handle=handle,
            managed=managed,
        )
        self._runs[request.run_id] = state
        # Also index the harness id so callers can inspect a handle returned by
        # a driver that does not use the durable run id as its own id.
        self._runs.setdefault(handle.id, state)
        self._journal.mark_run_started(
            run_id=request.run_id,
            start_hash=fingerprint,
        )
        self._start_attempts.pop(request.run_id, None)
        return {"handle": handle.to_dict(), "managed": managed}

    async def _observe(
        self,
        request: Envelope,
        state: _RunState,
    ) -> dict[str, Any]:
        self._expect_fields(request.payload, set())
        if not state.managed or not isinstance(state.driver, ManagedHarnessDriver):
            return {"observation": None}
        observation = state.driver.observe(request.run_id)
        state.last_observation = observation
        return {
            "observation": observation.to_dict() if observation is not None else None
        }

    def _run(self, run_id: str) -> _RunState:
        if not run_id:
            raise WorkerProtocolError("run action requires a run_id")
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise WorkerOperationError(f"unknown worker run {run_id!r}") from error

    def _expect_fields(
        self,
        payload: dict[str, Any],
        expected: set[str],
    ) -> dict[str, Any]:
        if len(payload) > self._max_payload_items:
            raise WorkerProtocolError("operation payload has too many fields")
        if set(payload) != expected:
            raise WorkerProtocolError("operation payload fields are invalid")
        return payload

    @staticmethod
    def _safe_error(error: Exception) -> str:
        detail = str(error).strip()
        if isinstance(error, WorkerProtocolError):
            return detail[:16_384] if detail else "operation rejected"
        return f"worker operation rejected: {type(error).__name__}"

    @staticmethod
    def _replay(entry: JournalEntry) -> OperationResponse:
        if (
            entry.status == "pending"
            or entry.response is None
            or entry.sequence is None
        ):
            raise WorkerOperationError("operation outcome is unknown")
        value = entry.response
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise WorkerProtocolError("journal response payload is invalid")
        ok = value.get("ok")
        if not isinstance(ok, bool):
            raise WorkerProtocolError("journal response status is invalid")
        error = value.get("error")
        if error is not None and not isinstance(error, str):
            raise WorkerProtocolError("journal response error is invalid")
        return OperationResponse(payload, ok, error, entry.sequence)


__all__ = ["ExecutionService", "OperationResponse"]
