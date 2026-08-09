"""Minimal asynchronous scheduler loop for embedding in a local daemon process."""

import asyncio
from collections.abc import Callable

from agentd.domain.models import RunRecord
from agentd.service import ControlPlane


class AgentDaemon:
    def __init__(
        self,
        control_plane: ControlPlane,
        *,
        poll_interval: float = 1,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._control_plane = control_plane
        self._poll_interval = poll_interval
        self._on_error = on_error
        self._last_error: Exception | None = None

    @property
    def last_error(self) -> Exception | None:
        """Most recent dispatch failure observed by the serving loop."""

        return self._last_error

    async def tick(self) -> RunRecord | None:
        return await self._control_plane.dispatch_next()

    async def serve(self, stop: asyncio.Event) -> None:
        """Dispatch ready work until ``stop`` is set.

        Running jobs finish through the agent-facing completion protocol; this
        loop owns admission and dispatch only.
        """

        while not stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                # Admission effects compensate independently. Keep the daemon
                # alive so a routine bad workspace/harness cannot stop unrelated
                # work from being considered on the next tick.
                self._last_error = error
                if self._on_error is not None:
                    self._on_error(error)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue
