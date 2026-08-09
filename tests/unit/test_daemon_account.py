from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agentd.daemon import AgentDaemon
from agentd.domain.enums import JobState, QuotaUnit
from agentd.domain.models import (
    EffortEstimate,
    Job,
    ProviderQuotaSnapshot,
    QuotaBudget,
)


def _job(identifier: str) -> Job:
    return Job(
        id=identifier,
        project="goldenage",
        repository="/srv/goldenage",
        objective="canary",
        effort=EffortEstimate(1, 2),
        quota_budget=QuotaBudget(
            implementation=100,
            maximum=150,
            pool_id="codex",
            unit=QuotaUnit.TOKENS,
        ),
        preferred_harnesses=("codex",),
        allowed_harnesses=("codex",),
        state=JobState.READY,
    )


@dataclass
class RecordingOracle:
    calls: int = 0

    async def snapshot(self) -> ProviderQuotaSnapshot:
        self.calls += 1
        return ProviderQuotaSnapshot(
            id=f"snapshot-{self.calls}",
            pool_id="codex",
            bucket_id="codex",
            primary_used_percent=25,
            observed_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        )


class RecordingPlane:
    def __init__(self) -> None:
        self.jobs = [_job("job-1")]
        self.snapshots: list[ProviderQuotaSnapshot] = []
        self.reconciled: list[ProviderQuotaSnapshot | None] = []
        self.dispatches = 0

    def list_jobs(self, _states=None):
        return list(self.jobs)

    def apply_provider_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    async def reconcile_managed_runs(self, snapshot, *, at):
        self.reconciled.append(snapshot)
        return ()

    async def dispatch_next(self):
        self.dispatches += 1
        return None


def test_daemon_refreshes_on_interval_and_before_each_new_admission() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 8, 9, 12, tzinfo=UTC)]
        plane = RecordingPlane()
        oracle = RecordingOracle()
        daemon = AgentDaemon(
            plane,
            account_oracle=oracle,
            account_poll_seconds=60,
            clock=lambda: now[0],
        )

        await daemon.tick()
        await daemon.tick()
        assert oracle.calls == 1

        plane.jobs.append(_job("job-2"))
        await daemon.tick()
        assert oracle.calls == 2

        now[0] += timedelta(seconds=60)
        await daemon.tick()
        assert oracle.calls == 3
        assert plane.snapshots[-1].id == "snapshot-3"
        assert plane.reconciled[-1] == plane.snapshots[-1]
        assert plane.dispatches == 4

    asyncio.run(scenario())


def test_daemon_contains_account_refresh_failure_and_keeps_dispatching() -> None:
    class FailingOracle:
        async def snapshot(self):
            raise RuntimeError("account telemetry unavailable")

    async def scenario() -> None:
        plane = RecordingPlane()
        observed: list[Exception] = []
        daemon = AgentDaemon(
            plane,
            account_oracle=FailingOracle(),
            on_error=observed.append,
        )

        assert await daemon.tick() is None
        assert plane.dispatches == 1
        assert len(observed) == 1
        assert str(observed[0]) == "account telemetry unavailable"

    asyncio.run(scenario())
