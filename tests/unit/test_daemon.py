from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from agentd.daemon import AgentDaemon
from agentd.service import ControlPlane


class FlakyControlPlane:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.calls = 0

    async def dispatch_next(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("routine dispatch failure")
        self._stop.set()


def test_daemon_contains_one_tick_failure_and_keeps_serving() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        plane = FlakyControlPlane(stop)
        observed: list[Exception] = []
        daemon = AgentDaemon(
            cast(ControlPlane, plane),
            poll_interval=0.001,
            dispatch_retry_base_seconds=0.001,
            on_error=observed.append,
        )

        await asyncio.wait_for(daemon.serve(stop), timeout=0.2)

        assert plane.calls == 2
        assert isinstance(daemon.last_error, RuntimeError)
        assert observed == [daemon.last_error]

    asyncio.run(scenario())


class RetryRecordingControlPlane:
    def __init__(self) -> None:
        self.dispatches = 0
        self.reconciliations = 0

    async def reconcile_managed_runs(self, _snapshot, *, at) -> tuple[()]:
        del at
        self.reconciliations += 1
        return ()

    async def dispatch_next(self) -> None:
        self.dispatches += 1
        raise RuntimeError("persistent dispatch failure")


def test_dispatch_failures_back_off_without_delaying_reconciliation() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 8, 9, 12, tzinfo=UTC)]
        plane = RetryRecordingControlPlane()
        daemon = AgentDaemon(
            cast(ControlPlane, plane),
            dispatch_retry_base_seconds=5,
            dispatch_retry_max_seconds=20,
            clock=lambda: now[0],
        )

        for expected_delay in (5, 10, 20, 20):
            try:
                await daemon.tick()
            except RuntimeError:
                pass
            else:
                raise AssertionError("dispatch failure was not propagated")

            now[0] += timedelta(seconds=expected_delay - 0.001)
            assert await daemon.tick() is None
            now[0] += timedelta(milliseconds=1)

        assert plane.dispatches == 4
        assert plane.reconciliations == 8

    asyncio.run(scenario())
