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
        account_oracle: AccountOracle | None = None,
        account_poll_seconds: float = 60,
        clock: Callable[[], datetime] = utc_now,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if account_poll_seconds <= 0:
            raise ValueError("account_poll_seconds must be positive")
        self._control_plane = control_plane
        self._poll_interval = poll_interval
        self._account_oracle = account_oracle
        self._account_poll = timedelta(seconds=account_poll_seconds)
        self._clock = clock
        self._on_error = on_error
        self._last_error: Exception | None = None
        self._last_account_refresh: datetime | None = None
        self._account_snapshot: ProviderQuotaSnapshot | None = None
        self._refreshed_ready_jobs: set[str] = set()

    @property
    def last_error(self) -> Exception | None:
        """Most recent dispatch failure observed by the serving loop."""

        return self._last_error

    async def tick(self) -> RunRecord | None:
        now = self._clock()
        ready_codex = self._ready_codex_jobs()
        self._refreshed_ready_jobs.intersection_update(ready_codex)
        has_new_admission = not ready_codex.issubset(self._refreshed_ready_jobs)
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
                self._refreshed_ready_jobs.update(ready_codex)
        reconcile = getattr(self._control_plane, "reconcile_managed_runs", None)
        if callable(reconcile):
            await reconcile(self._account_snapshot, at=now)
        return await self._control_plane.dispatch_next()

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

    def _ready_codex_jobs(self) -> set[str]:
        list_jobs = getattr(self._control_plane, "list_jobs", None)
        if not callable(list_jobs):
            return set()
        return {
            job.id
            for job in list_jobs(frozenset({JobState.READY}))
            if "codex" in job.allowed_harnesses
        }
