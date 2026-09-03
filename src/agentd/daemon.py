"""Minimal asynchronous scheduler loop for embedding in a local daemon process."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
from typing import Protocol, runtime_checkable

from agentd.domain.enums import JobState
from agentd.domain.models import Job, ProviderQuotaSnapshot, RunRecord, utc_now
from agentd.observability import event_logger
from agentd.runtime.codex_oracle import AccountOracle
from agentd.runtime.reset import detect_provider_reset, reset_event_for_decision
from agentd.service import ControlPlane
from agentd.state.base import StateStore


@runtime_checkable
class _RecoverableControlPlane(Protocol):
    async def recover_managed_runs(self) -> None: ...


@runtime_checkable
class _ReconcilableControlPlane(Protocol):
    async def reconcile_managed_runs(
        self,
        snapshot: ProviderQuotaSnapshot | None = None,
        *,
        at: datetime | None = None,
    ) -> tuple[object, ...]: ...


@runtime_checkable
class _HeartbeatControlPlane(Protocol):
    async def refresh_worker_heartbeats(self) -> tuple[dict[str, object], ...]: ...


@runtime_checkable
class _AdmissionInspectableControlPlane(Protocol):
    @property
    def store(self) -> StateStore: ...

    def list_jobs(self, states: frozenset[JobState] | None = None) -> list[Job]: ...


class AgentDaemon:
    """Poll provider state, reconcile managed runs, and dispatch ready work."""

    def __init__(
        self,
        control_plane: ControlPlane,
        *,
        poll_interval: float = 1,
        dispatch_retry_base_seconds: float = 5,
        dispatch_retry_max_seconds: float = 60,
        account_oracle: AccountOracle | None = None,
        account_poll_seconds: float = 60,
        worker_heartbeat_seconds: float = 15,
        provider_reset_remaining: float | None = None,
        clock: Callable[[], datetime] = utc_now,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if dispatch_retry_base_seconds <= 0:
            raise ValueError("dispatch_retry_base_seconds must be positive")
        if dispatch_retry_max_seconds < dispatch_retry_base_seconds:
            raise ValueError(
                "dispatch_retry_max_seconds must be at least the retry base"
            )
        if account_poll_seconds <= 0:
            raise ValueError("account_poll_seconds must be positive")
        if worker_heartbeat_seconds <= 0:
            raise ValueError("worker_heartbeat_seconds must be positive")
        if provider_reset_remaining is not None and (
            not isfinite(provider_reset_remaining) or provider_reset_remaining < 0
        ):
            raise ValueError("provider_reset_remaining must be finite and non-negative")
        self._control_plane = control_plane
        self._poll_interval = poll_interval
        self._dispatch_retry_base_seconds = dispatch_retry_base_seconds
        self._dispatch_retry_max_seconds = dispatch_retry_max_seconds
        self._dispatch_retry_at: datetime | None = None
        self._dispatch_failures = 0
        self._account_oracle = account_oracle
        self._account_poll = timedelta(seconds=account_poll_seconds)
        self._worker_heartbeat_poll = timedelta(seconds=worker_heartbeat_seconds)
        self._provider_reset_remaining = provider_reset_remaining
        self._clock = clock
        self._on_error = on_error
        self._last_error: Exception | None = None
        self._last_account_refresh: datetime | None = None
        self._last_worker_heartbeat: datetime | None = None
        self._account_snapshot: ProviderQuotaSnapshot | None = None
        self._refreshed_admissions: set[str] = set()

    @property
    def last_error(self) -> Exception | None:
        """Most recent dispatch failure observed by the serving loop."""

        return self._last_error

    async def tick(self) -> RunRecord | None:
        now = self._clock()
        await self._refresh_worker_heartbeats(now)
        admission_keys = self._codex_admission_keys()
        self._refreshed_admissions.intersection_update(admission_keys)
        has_new_admission = not admission_keys.issubset(self._refreshed_admissions)
        if self._account_oracle is not None and (
            self._last_account_refresh is None
            or now - self._last_account_refresh >= self._account_poll
            or has_new_admission
        ):
            try:
                current_snapshot = await self._account_oracle.snapshot()
                previous_snapshot = self._matching_previous_provider_snapshot(
                    current_snapshot
                )
                if previous_snapshot is not None:
                    decision = detect_provider_reset(
                        previous_snapshot,
                        current_snapshot,
                        at=now,
                    )
                    if decision.confirmed:
                        event = reset_event_for_decision(
                            decision,
                            new_remaining=self._provider_reset_remaining,
                        )
                        if event is None:
                            event_logger(
                                component="account_oracle",
                                pool_id=decision.pool_id,
                                reset_event_id=decision.event_id,
                            ).warning(
                                "provider_reset_detected_without_absolute_capacity"
                            )
                        else:
                            self._control_plane.register_quota_event(event)
                            event_logger(
                                component="account_oracle",
                                pool_id=decision.pool_id,
                                reset_event_id=decision.event_id,
                            ).info("provider_reset_applied")
                self._account_snapshot = current_snapshot
                self._control_plane.apply_provider_snapshot(current_snapshot)
                self._last_account_refresh = now
                event_logger(component="account_oracle").info(
                    "provider_quota_refreshed"
                )
            except Exception as error:
                # Existing durable telemetry remains authoritative until it
                # becomes stale; the coordinator then blocks nonurgent work.
                self._record_error(error, operation="provider_quota_refresh")
            finally:
                self._last_account_refresh = now
                self._refreshed_admissions.update(admission_keys)
        if isinstance(self._control_plane, _ReconcilableControlPlane):
            await self._control_plane.reconcile_managed_runs(
                self._account_snapshot, at=now
            )
        if self._dispatch_retry_at is not None and now < self._dispatch_retry_at:
            return None
        try:
            dispatched = await self._control_plane.dispatch_next()
        except Exception:
            self._dispatch_failures += 1
            exponent = min(self._dispatch_failures - 1, 30)
            delay_seconds = min(
                self._dispatch_retry_max_seconds,
                self._dispatch_retry_base_seconds * (2**exponent),
            )
            self._dispatch_retry_at = now + timedelta(seconds=delay_seconds)
            raise
        self._dispatch_failures = 0
        self._dispatch_retry_at = None
        return dispatched

    def _matching_previous_provider_snapshot(
        self,
        current: ProviderQuotaSnapshot,
    ) -> ProviderQuotaSnapshot | None:
        """Return the newest older snapshot from the exact provider bucket.

        An oracle may persist ``current`` before returning it. Filtering by
        identity, timestamp, and ID both excludes that row and prevents a newer
        snapshot from another pool from hiding reset evidence after restart.
        """

        def precedes(snapshot: ProviderQuotaSnapshot) -> bool:
            return (
                snapshot.provider == current.provider
                and snapshot.pool_id == current.pool_id
                and snapshot.bucket_id == current.bucket_id
                and snapshot.id != current.id
                and snapshot.observed_at < current.observed_at
            )

        in_memory = self._account_snapshot
        if in_memory is not None and precedes(in_memory):
            # One active daemon owns polling. Its last matching observation is
            # already the newest baseline and avoids scanning durable history
            # on every regular poll.
            return in_memory
        if not isinstance(self._control_plane, _AdmissionInspectableControlPlane):
            return None
        list_snapshots = getattr(
            self._control_plane.store,
            "list_provider_quota_snapshots",
            None,
        )
        if list_snapshots is None:
            return None
        persisted = list(list_snapshots(current.pool_id, current.bucket_id))
        matching = [snapshot for snapshot in persisted if precedes(snapshot)]
        return max(
            matching,
            key=lambda item: (item.observed_at, item.id),
            default=None,
        )

    async def serve(self, stop: asyncio.Event) -> None:
        """Dispatch ready work until ``stop`` is set.

        Running jobs finish through the agent-facing completion protocol; this
        loop owns admission and dispatch only.
        """

        if not await self._recover_before_serving(stop):
            return

        while not stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                # Admission effects compensate independently. Keep the daemon
                # alive so a routine bad workspace/harness cannot stop unrelated
                # work from being considered on the next tick.
                self._record_error(error, operation="dispatch_tick")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def _recover_before_serving(self, stop: asyncio.Event) -> bool:
        """Recover durable managed runs before admitting any new work.

        Startup transport failures can be transient.  Retrying with a capped
        exponential delay keeps existing allocations from being stranded while
        also preventing the daemon from entering its normal dispatch loop before
        recovery has succeeded.
        """

        if not isinstance(self._control_plane, _RecoverableControlPlane):
            return True
        delay_seconds = self._dispatch_retry_base_seconds
        while not stop.is_set():
            try:
                await self._refresh_worker_heartbeats(self._clock(), force=True)
                await self._control_plane.recover_managed_runs()
            except Exception as error:
                self._record_error(error, operation="managed_run_recovery")
            else:
                return True
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
            except TimeoutError:
                delay_seconds = min(
                    self._dispatch_retry_max_seconds,
                    delay_seconds * 2,
                )
            else:
                return False
        return False

    async def _refresh_worker_heartbeats(
        self,
        now: datetime,
        *,
        force: bool = False,
    ) -> None:
        if not isinstance(self._control_plane, _HeartbeatControlPlane):
            return
        if (
            not force
            and self._last_worker_heartbeat is not None
            and now - self._last_worker_heartbeat < self._worker_heartbeat_poll
        ):
            return
        # Record the attempt even on failure. A broken worker must not turn the
        # daemon's normal one-second tick into a connection storm.
        self._last_worker_heartbeat = now
        try:
            await self._control_plane.refresh_worker_heartbeats()
        except Exception as error:
            self._record_error(error, operation="worker_heartbeat_refresh")

    def _record_error(self, error: Exception, *, operation: str) -> None:
        self._last_error = error
        event_logger(
            component="daemon",
            operation=operation,
            error_type=type(error).__name__,
        ).error("operation_failed")
        if self._on_error is not None:
            self._on_error(error)

    def _codex_admission_keys(self) -> set[str]:
        if not isinstance(self._control_plane, _AdmissionInspectableControlPlane):
            return set()
        keys = {
            f"ready:{job.id}"
            for job in self._control_plane.list_jobs(frozenset({JobState.READY}))
            if "codex" in job.allowed_harnesses
        }
        for command in self._control_plane.store.list_pending_run_commands():
            if command.action != "repair":
                continue
            try:
                run = self._control_plane.store.get_run(command.run_id)
            except LookupError:
                continue
            if run.driver == "codex":
                keys.add(f"repair:{command.id}")
        return keys
