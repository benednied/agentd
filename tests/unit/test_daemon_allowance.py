import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from agentd.daemon import AgentDaemon
from agentd.domain.enums import QuotaUnit
from agentd.domain.models import QuotaPool
from agentd.runtime.allowance import LocalAllowancePolicy
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


class Plane:
    def __init__(self, store):
        self.store = store
        self.dispatches = 0
        self.reconciliations = 0

    def list_jobs(self, _states=None):
        return []

    async def reconcile_managed_runs(self, _snapshot, *, at):
        self.reconciliations += 1
        return ()

    async def dispatch_next(self):
        self.dispatches += 1
        return None


def test_daemon_reconciles_approved_allowance_without_provider_conversion():
    async def scenario():
        with SQLiteStateStore() as store:
            store.save_quota_pool(
                QuotaPool("codex", "openai", 1, unit=QuotaUnit.TOKENS)
            )
            plane = Plane(store)
            now = [NOW]
            daemon = AgentDaemon(
                cast(ControlPlane, plane),
                local_allowance_policy=LocalAllowancePolicy("codex", "daily", 500),
                clock=lambda: now[0],
            )
            await daemon.tick()
            pool = store.get_quota_pool("codex")
            assert pool.remaining == 500
            store.update_quota_pool(pool, replace(pool, remaining=2))
            await daemon.tick()
            assert store.get_quota_pool("codex").remaining == 2
            now[0] += timedelta(days=1)
            await daemon.tick()
            assert store.get_quota_pool("codex").remaining == 500
            assert store.latest_provider_quota_snapshot("codex") is None
            assert len(store.list_reset_events("codex")) == 2
            assert plane.reconciliations == plane.dispatches == 3

    asyncio.run(scenario())


def test_failed_allowance_refresh_still_collects_but_blocks_admission():
    async def scenario():
        with SQLiteStateStore() as store:
            store.save_quota_pool(
                QuotaPool("codex", "openai", 1, unit=QuotaUnit.TOKENS)
            )
            policy = LocalAllowancePolicy("codex", "daily", 500)
            policy.reconcile(store, at=NOW)
            plane = Plane(store)
            observed = []
            daemon = AgentDaemon(
                cast(ControlPlane, plane),
                local_allowance_policy=replace(policy, tokens_per_window=600),
                clock=lambda: NOW + timedelta(days=1),
                on_error=observed.append,
            )
            assert await daemon.tick() is None
            assert plane.reconciliations == 1
            assert plane.dispatches == 0
            assert len(observed) == 1

    asyncio.run(scenario())


def test_startup_ownership_blocker_can_report_without_admitting_or_resuming():
    async def scenario():
        stop = asyncio.Event()
        reports = []
        results = []

        class UnresolvedPlane:
            dispatches = 0

            async def recover_managed_runs(self):
                raise RuntimeError("provider ownership is unresolved")

            async def dispatch_next(self):
                self.dispatches += 1

        async def reporter(error):
            reports.append(str(error))
            stop.set()

        async def reconcile_results():
            results.append("unexpected publication or resume")

        plane = UnresolvedPlane()
        daemon = AgentDaemon(
            cast(ControlPlane, plane),
            recovery_reporter=reporter,
            result_reconciler=reconcile_results,
            dispatch_retry_base_seconds=0.001,
        )
        await asyncio.wait_for(daemon.serve(stop), timeout=0.2)
        assert reports == ["provider ownership is unresolved"]
        assert plane.dispatches == 0
        assert results == []

    asyncio.run(scenario())
