"""Minimal asynchronous scheduler loop for embedding in a local daemon process."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from agentd.domain.enums import JobState
from agentd.domain.models import ProviderQuotaSnapshot, RunRecord, utc_now
from agentd.runtime.codex_oracle import AccountOracle
from agentd.service import ControlPlane


class AgentDaemon:
    def __init__(
        self,
        control_plane: ControlPlane,
        *,
        poll_interval: float = 1,
        dispatch_retry_base_seconds: float = 5,
        dispatch_retry_max_seconds: float = 60,
        account_oracle: AccountOracle | None = None,
        account_poll_seconds: float = 60,
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
        self._control_plane = control_plane
        self._poll_interval = poll_interval
        self._dispatch_retry_base_seconds = dispatch_retry_base_seconds
        self._dispatch_retry_max_seconds = dispatch_retry_max_seconds
        self._dispatch_retry_at: datetime | None = None
        self._dispatch_failures = 0
        self._account_oracle = account_oracle
        self._account_poll = timedelta(seconds=account_poll_seconds)
        self._clock = clock
        self._on_error = on_error
        self._last_error: Exception | None = None
        self._last_account_refresh: datetime | None = None
        self._account_snapshot: ProviderQuotaSnapshot | None = None
        self._refreshed_admissions: set[str] = set()

    @property
    def last_error(self) -> Exception | None:
        """Most recent dispatch failure observed by the serving loop."""

        return self._last_error

    async def tick(self) -> RunRecord | None:
        now = self._clock()
        admission_keys = self._codex_admission_keys()
        self._refreshed_admissions.intersection_update(admission_keys)
        has_new_admission = not admission_keys.issubset(self._refreshed_admissions)
        if self._account_oracle is not None and (
            self._last_account_refresh is None
            or now - self._last_account_refresh >= self._account_poll
            or has_new_admission
        ):
            try:
                self._account_snapshot = await self._account_oracle.snapshot()
                self._control_plane.apply_provider_snapshot(self._account_snapshot)
                self._last_account_refresh = now
            except Exception as error:
                # Existing durable telemetry remains authoritative until it
                # becomes stale; the coordinator then blocks nonurgent work.
                self._record_error(error)
            finally:
                self._last_account_refresh = now
                self._refreshed_admissions.update(admission_keys)
        reconcile = getattr(self._control_plane, "reconcile_managed_runs", None)
        if callable(reconcile):
            await reconcile(self._account_snapshot, at=now)
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

    async def serve(self, stop: asyncio.Event) -> None:
        """Dispatch ready work until ``stop`` is set.

        Running jobs finish through the agent-facing completion protocol; this
        loop owns admission and dispatch only.
        """

        recover = getattr(self._control_plane, "recover_managed_runs", None)
        if callable(recover):
            try:
                await recover()
            except Exception as error:
                self._record_error(error)

        while not stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                # Admission effects compensate independently. Keep the daemon
                # alive so a routine bad workspace/harness cannot stop unrelated
                # work from being considered on the next tick.
                self._record_error(error)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    def _record_error(self, error: Exception) -> None:
        self._last_error = error
        if self._on_error is not None:
            self._on_error(error)

    def _codex_admission_keys(self) -> set[str]:
        list_jobs = getattr(self._control_plane, "list_jobs", None)
        if not callable(list_jobs):
            return set()
        keys = {
            f"ready:{job.id}"
            for job in list_jobs(frozenset({JobState.READY}))
            if "codex" in job.allowed_harnesses
        }
        store = getattr(self._control_plane, "store", None)
        list_pending = getattr(store, "list_pending_run_commands", None)
        get_run = getattr(store, "get_run", None)
        if not callable(list_pending) or not callable(get_run):
            return keys
        for command in list_pending():
            if command.action != "repair":
                continue
            try:
                run = get_run(command.run_id)
            except LookupError:
                continue
            if run.driver == "codex":
                keys.add(f"repair:{command.id}")
        return keys
