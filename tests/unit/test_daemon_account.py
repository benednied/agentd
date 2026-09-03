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
    QuotaPool,
    QuotaResetEvent,
    RunCommand,
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
        self.store = self
        self.snapshots: list[ProviderQuotaSnapshot] = []
        self.reconciled: list[ProviderQuotaSnapshot | None] = []
        self.dispatches = 0
        self.pending_commands: list[RunCommand] = []
        self.reset_events: list[QuotaResetEvent] = []
        self.durable_snapshots: list[ProviderQuotaSnapshot] = []

    def list_jobs(self, _states=None):
        return list(self.jobs)

    def apply_provider_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    def register_quota_event(self, event):
        self.reset_events.append(event)
        return event

    async def reconcile_managed_runs(self, snapshot, *, at):
        self.reconciled.append(snapshot)
        return ()

    async def dispatch_next(self):
        self.dispatches += 1
        return None

    def list_pending_run_commands(self):
        return list(self.pending_commands)

    def list_quota_pools(self):
        return [
            QuotaPool(id=pool_id, provider="openai", remaining=100)
            for pool_id in sorted({item.pool_id for item in self.durable_snapshots})
        ]

    def latest_provider_quota_snapshot(self, pool_id, bucket_id=None):
        snapshots = self.list_provider_quota_snapshots(pool_id, bucket_id)
        return snapshots[-1] if snapshots else None

    def list_provider_quota_snapshots(self, pool_id, bucket_id=None):
        return sorted(
            (
                item
                for item in self.durable_snapshots
                if item.pool_id == pool_id
                and (bucket_id is None or item.bucket_id == bucket_id)
            ),
            key=lambda item: (item.observed_at, item.id),
        )

    @staticmethod
    def get_run(run_id):
        return type("Run", (), {"id": run_id, "driver": "codex"})()


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

        plane.pending_commands.append(
            RunCommand(
                id="repair-turn:job-1:1",
                run_id="run-1",
                action="repair",
            )
        )
        await daemon.tick()
        assert oracle.calls == 3

        now[0] += timedelta(seconds=60)
        await daemon.tick()
        assert oracle.calls == 4
        assert plane.snapshots[-1].id == "snapshot-4"
        assert plane.reconciled[-1] == plane.snapshots[-1]
        assert plane.dispatches == 5

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


def test_daemon_applies_one_evidenced_reset_with_explicit_absolute_capacity() -> None:
    class ResetOracle:
        def __init__(self) -> None:
            self.calls = 0

        async def snapshot(self) -> ProviderQuotaSnapshot:
            self.calls += 1
            observed = datetime(2026, 8, 9, 12, tzinfo=UTC) + timedelta(
                minutes=self.calls - 1
            )
            return ProviderQuotaSnapshot(
                id=f"reset-snapshot-{self.calls}",
                pool_id="codex",
                bucket_id="codex",
                primary_used_percent=95 if self.calls == 1 else 5,
                primary_reset_at=(
                    datetime(2026, 8, 9, 12, 30, tzinfo=UTC)
                    if self.calls == 1
                    else datetime(2026, 8, 9, 17, 30, tzinfo=UTC)
                ),
                observed_at=observed,
            )

    async def scenario() -> None:
        now = [datetime(2026, 8, 9, 12, tzinfo=UTC)]
        plane = RecordingPlane()
        daemon = AgentDaemon(
            plane,
            account_oracle=ResetOracle(),
            account_poll_seconds=60,
            provider_reset_remaining=750_000,
            clock=lambda: now[0],
        )

        await daemon.tick()
        assert plane.reset_events == []
        now[0] += timedelta(minutes=1)
        await daemon.tick()
        assert len(plane.reset_events) == 1
        assert plane.reset_events[0].new_remaining == 750_000

        now[0] += timedelta(minutes=1)
        await daemon.tick()
        assert len(plane.reset_events) == 1

    asyncio.run(scenario())


def test_daemon_detects_but_does_not_invent_reset_capacity() -> None:
    class ResetOracle:
        def __init__(self) -> None:
            self.calls = 0

        async def snapshot(self) -> ProviderQuotaSnapshot:
            self.calls += 1
            return ProviderQuotaSnapshot(
                id=f"snapshot-{self.calls}",
                pool_id="codex",
                bucket_id="codex",
                primary_used_percent=90 if self.calls == 1 else 10,
                primary_reset_at=datetime(
                    2026,
                    8,
                    9,
                    13 + self.calls,
                    tzinfo=UTC,
                ),
                observed_at=datetime(2026, 8, 9, 12, self.calls - 1, tzinfo=UTC),
            )

    async def scenario() -> None:
        now = [datetime(2026, 8, 9, 12, tzinfo=UTC)]
        plane = RecordingPlane()
        daemon = AgentDaemon(
            plane,
            account_oracle=ResetOracle(),
            account_poll_seconds=60,
            clock=lambda: now[0],
        )

        await daemon.tick()
        now[0] += timedelta(minutes=1)
        await daemon.tick()
        assert plane.reset_events == []

    asyncio.run(scenario())


def test_daemon_restart_uses_reset_baseline_from_the_same_pool_and_bucket() -> None:
    current = ProviderQuotaSnapshot(
        id="pool-a-after-reset",
        provider="openai",
        pool_id="pool-a",
        bucket_id="five-hour",
        primary_used_percent=5,
        primary_reset_at=datetime(2026, 8, 9, 18, tzinfo=UTC),
        observed_at=datetime(2026, 8, 9, 12, 10, tzinfo=UTC),
    )

    class CurrentOracle:
        async def snapshot(self) -> ProviderQuotaSnapshot:
            return current

    async def scenario() -> None:
        plane = RecordingPlane()
        plane.durable_snapshots.extend(
            (
                ProviderQuotaSnapshot(
                    id="pool-a-before-reset",
                    provider="openai",
                    pool_id="pool-a",
                    bucket_id="five-hour",
                    primary_used_percent=95,
                    primary_reset_at=datetime(2026, 8, 9, 13, tzinfo=UTC),
                    observed_at=datetime(2026, 8, 9, 12, 8, tzinfo=UTC),
                ),
                ProviderQuotaSnapshot(
                    id="newer-unrelated-pool-b",
                    provider="openai",
                    pool_id="pool-b",
                    bucket_id="weekly",
                    primary_used_percent=50,
                    primary_reset_at=datetime(2026, 8, 16, tzinfo=UTC),
                    observed_at=datetime(2026, 8, 9, 12, 9, tzinfo=UTC),
                ),
            )
        )
        daemon = AgentDaemon(
            plane,
            account_oracle=CurrentOracle(),
            provider_reset_remaining=500,
            clock=lambda: current.observed_at,
        )

        await daemon.tick()

        assert len(plane.reset_events) == 1
        assert plane.reset_events[0].pool_id == "pool-a"
        assert plane.reset_events[0].new_remaining == 500

    asyncio.run(scenario())
