from __future__ import annotations

import asyncio
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
            on_error=observed.append,
        )

        await asyncio.wait_for(daemon.serve(stop), timeout=0.2)

        assert plane.calls == 2
        assert isinstance(daemon.last_error, RuntimeError)
        assert observed == [daemon.last_error]

    asyncio.run(scenario())
