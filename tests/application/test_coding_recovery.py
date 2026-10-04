"""Coding transport ownership and quota policy across controller recovery."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_github_coding_pipeline import ControlledProvider, FixtureClone, git

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.descriptor import RemoteCodingDescriptor
from agentd.coding.models import RepositoryProfile
from agentd.coordinator import LifecycleError, SchedulerCoordinator
from agentd.daemon import AgentDaemon
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome, RunState
from agentd.domain.models import (
    EffortEstimate,
    Job,
    ProviderQuotaSnapshot,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    RunObservation,
    RunResult,
    TokenUsage,
    WorkerNode,
    utc_now,
)
from agentd.harness.registry import DriverRegistry
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.runtime.accounts import AccountPolicyThresholds
from agentd.runtime.governor import ProviderStopPolicy
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers import (
    BackendRegistry,
    OperationJournal,
    RemoteWorkerBackend,
    RemoteWorkerClient,
    WorkerServer,
)
from agentd.workers.coding import CodingHarnessDriver
from agentd.workers.errors import WorkerStartUncertainError
from agentd.workspaces.git import GitWorkspaceManager


class PausedProvider(ControlledProvider):
    def __init__(self):
        self.starts = 0
        self.cancels = 0
        self.finished = asyncio.Event()

    def observe(self, run_id):
        result = RunResult(
            RunOutcome.CANCELLED,
            "bounded stop",
            usage=TokenUsage(input_tokens=12),
            consumed_quota=12,
            metadata={"telemetry_valid": True},
        )
        return RunObservation(
            run_id,
            "provider-thread",
            "provider-turn",
            "usage-12",
            cumulative_quota=12,
            unit=QuotaUnit.TOKENS,
            usage=result.usage,
            terminal=self.finished.is_set(),
            result=result if self.finished.is_set() else None,
        )

    async def collect(self, run):
        await self.finished.wait()
        return self.observe(run.id).result

    async def cancel(self, run):
        self.cancels += 1
        self.finished.set()


class LostStartAckBackend(RemoteWorkerBackend):
    async def dispatch(self, *args, **kwargs):
        await super().dispatch(*args, **kwargs)
        raise WorkerStartUncertainError(
            "worker started; controller acknowledgement lost"
        )


def test_legacy_recovery_preserves_terminal_run_and_requires_matching_evidence(
    tmp_path,
):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            rig.quota()
            run = await rig.plane.dispatch_next()
            path = Path(rig.provider.execution.working_directory)
            (path / "retained.txt").write_text("partial implementation\n")
            await rig.worker.cancel(rig.worker._runs[run.id].handle)
            await rig.coordinator.reconcile_managed_runs()
            original = rig.store.get_run(run.id)
            assert rig.store.get_job(rig.job.id).state is JobState.CANCELLED
            checkpoint = await rig.worker.capture_checkpoint(run.id)
            with pytest.raises(LifecycleError, match="identity mismatch"):
                rig.coordinator.restore_coding_checkpoint(
                    rig.job.id, {**checkpoint, "job_id": "other"}, actor="operator"
                )
            recovered = rig.coordinator.restore_coding_checkpoint(
                rig.job.id, checkpoint, actor="operator"
            )
            assert recovered.state is JobState.SUSPENDED
            assert rig.store.get_run(run.id) == original
            await rig.coordinator.resume(
                rig.job.id, maximum_tokens=300, actor="operator"
            )
            assert rig.store.get_job(rig.job.id).quota_budget.maximum == 300
            assert rig.store.get_quota_pool("account").remaining == 988
            assert rig.provider.starts == 1

    asyncio.run(scenario())


@dataclass
class RecoveryRig:
    path: Path
    profile: RepositoryProfile
    provider: PausedProvider
    server: WorkerServer
    worker: CodingHarnessDriver
    store: SQLiteStateStore
    client: RemoteWorkerClient
    backend: RemoteWorkerBackend
    coordinator: SchedulerCoordinator
    plane: ControlPlane
    job: Job

    async def reopen_controller(self):
        self.store.close()
        await self.client.close()
        self.store = SQLiteStateStore(self.path / "controller.sqlite")
        self.client, self.backend, self.coordinator, self.plane = _controller(
            self.path, self.store, self.server, self.worker, RemoteWorkerBackend
        )
        await self.plane.refresh_worker_heartbeats()

    def quota(self, used=10, **kwargs):
        self.store.append_provider_quota_snapshot(
            ProviderQuotaSnapshot(
                pool_id="account",
                bucket_id="account",
                primary_used_percent=used,
                **kwargs,
            )
        )


def _controller(path, store, server, worker, backend_type, **policies):
    host, port = server.address
    client = RemoteWorkerClient(
        host,
        port,
        node_id="worker",
        session_epoch="epoch",
        secret=b"q" * 32,
        allow_insecure_loopback=True,
    )
    backend = backend_type(
        client, name="remote", node_id="worker", expected_driver="remote-coding"
    )
    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(path / "unused-controller-worktrees"),
        DriverRegistry([RemoteCodingDescriptor(worker.capabilities().features)]),
        backends=BackendRegistry([backend]),
        **policies,
    )
    return client, backend, coordinator, ControlPlane(store, coordinator=coordinator)


@asynccontextmanager
async def coding_rig(
    path,
    *,
    lose_ack=False,
    provider=None,
    validation_commands=(("true",),),
    quota_basis="total-v1",
    maximum_run_quota=None,
    **policies,
):
    repo = path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    (repo / "README").write_text("base\n")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "commit",
        "-m",
        "Base",
    )
    profile = RepositoryProfile(
        "repo",
        "v1",
        "test/repo",
        "https://github.com/test/repo.git",
        validation_commands=validation_commands,
    )
    compiler = CodingJobCompiler(
        profile,
        git(repo, "rev-parse", "HEAD"),
        QuotaBudget(100, maximum=200, pool_id="account", unit=QuotaUnit.TOKENS),
        EffortEstimate(1, 2),
        quota_basis=quota_basis,
        maximum_run_quota=maximum_run_quota,
    )
    issue = SourceIssue(
        "test/repo",
        7,
        1,
        "I_1",
        "Make change",
        "Intent",
        "2026-09-18T10:00:00Z",
        labels=("agentd:approved",),
    )
    store = SQLiteStateStore(path / "controller.sqlite")
    provider = provider or PausedProvider()
    worker = CodingHarnessDriver(
        path / "worker",
        {"repo": profile},
        {"codex": provider},
        runner=FixtureClone(repo),
        account_pools={"codex": "account"},
    )
    journal = OperationJournal(
        path / "worker.sqlite", node_id="worker", session_epoch="epoch"
    )
    server = WorkerServer(
        "127.0.0.1",
        0,
        node_id="worker",
        session_epoch="epoch",
        secret=b"q" * 32,
        drivers=[worker],
        journal=journal,
        allow_insecure_loopback=True,
    )
    await server.start()
    client, backend, coordinator, plane = _controller(
        path,
        store,
        server,
        worker,
        LostStartAckBackend if lose_ack else RemoteWorkerBackend,
        **policies,
    )
    plane.register_node(
        WorkerNode(
            "worker",
            capacity=ResourceVector(2, 2),
            harnesses=frozenset({"remote-coding"}),
            labels={"backend": "remote"},
        )
    )
    plane.register_quota_pool(
        QuotaPool("account", "codex", 1000, unit=QuotaUnit.TOKENS)
    )
    policy = IntakePolicy("test/repo", 7)
    store.observe_github_issue(issue, policy)
    store.approve_github_issue(issue, policy, actor="operator")
    job = store.create_github_job(issue, compiler(issue))
    rig = RecoveryRig(
        path,
        profile,
        provider,
        server,
        worker,
        store,
        client,
        backend,
        coordinator,
        plane,
        job,
    )
    try:
        await plane.refresh_worker_heartbeats()
        yield rig
    finally:
        for state in worker._runs.values():
            await worker.cancel(state.handle)
        await rig.client.close()
        await server.close()
        journal.close()
        rig.store.close()


def test_coding_lost_start_ack_reconnect_restart_charges_one_run(tmp_path):
    async def scenario():
        async with coding_rig(tmp_path, lose_ack=True) as rig:
            rig.quota()
            with pytest.raises(WorkerStartUncertainError):
                await rig.plane.dispatch_next()
            run = rig.store.latest_run(rig.job.id)
            reservation = rig.store.find_active_reservation(rig.job.id)
            assert run.state is RunState.STARTING
            assert rig.store.get_job(rig.job.id).state is JobState.ADMITTED
            assert rig.provider.starts == 1
            await rig.reopen_controller()
            await rig.coordinator.recover_managed_runs()
            await rig.coordinator.reconcile_managed_runs()
            await rig.coordinator.reconcile_managed_runs()
            assert rig.store.get_driver_session(run.id).observation_cursor == "usage-12"
            assert rig.store.get_quota_pool("account").remaining == 988
            assert rig.store.find_active_reservation(rig.job.id).id == reservation.id
            assert len(rig.store.list_usage_samples(run.id)) == 1
            assert len(rig.store.list_runs(rig.job.id)) == rig.provider.starts == 1
            assert rig.store.get_run(run.id).state is RunState.RUNNING
            assert await rig.plane.dispatch_next() is None

    asyncio.run(scenario())


def test_coding_stale_admission_and_midrun_pressure_survive_restart(tmp_path):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            assert await rig.plane.dispatch_next() is None
            rig.quota(observed_at=utc_now() - timedelta(minutes=6))
            assert await rig.plane.dispatch_next() is None
            assert rig.coordinator.quota_wait_reason(rig.job.id) == "quota_stale"
            rig.quota(75)
            assert await rig.plane.dispatch_next() is None
            rig.quota(10)
            run = await rig.plane.dispatch_next()
            assert run is not None
            rig.quota(90)
            await rig.coordinator.reconcile_managed_runs()
            commands = rig.store.list_run_commands(run.id)
            assert len(commands) == 1
            assert commands[0].action == "checkpoint"
            assert commands[0].id.startswith("provider-quota-checkpoint:" + run.id)
            assert rig.provider.cancels >= 1
            assert rig.store.get_job(rig.job.id).state is JobState.SUSPENDED
            assert rig.store.get_quota_pool("account").remaining == 988
            await rig.reopen_controller()
            await rig.coordinator.recover_managed_runs()
            await rig.coordinator.reconcile_managed_runs()
            assert rig.store.list_run_commands(run.id) == commands
            assert rig.provider.starts == len(rig.store.list_runs(rig.job.id)) == 1
            assert await rig.plane.dispatch_next() is None
            # Fresh reset evidence allows another bounded attempt, preserving
            # the same job, checkpoint and charged prior usage.
            rig.quota(10)
            await rig.coordinator.resume(rig.job.id)
            rig.provider.finished = asyncio.Event()
            await rig.plane.refresh_worker_heartbeats()
            resumed = await rig.plane.dispatch_next()
            assert resumed is not None and resumed.id != run.id
            assert resumed.contract.operation.work_order.resume_from_run_id == run.id
            assert resumed.contract.operation.work_order.prior_consumed_quota == 12
            assert rig.store.get_quota_pool("account").remaining == 988
            assert rig.provider.starts == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("lose_ack", [False, True])
def test_unknown_coding_ownership_retains_account_and_worker_claim(tmp_path, lose_ack):
    async def scenario():
        async with coding_rig(tmp_path, lose_ack=lose_ack) as rig:
            rig.quota()
            if lose_ack:
                with pytest.raises(WorkerStartUncertainError):
                    await rig.plane.dispatch_next()
            else:
                await rig.plane.dispatch_next()
            run = rig.store.latest_run(rig.job.id)
            reservation = rig.store.find_active_reservation(rig.job.id)
            allocation = rig.store.find_active_allocation(rig.job.id)
            await rig.reopen_controller()

            async def unknown(_run):
                return {"known": False, "terminal": False, "result": None}

            rig.backend.status = unknown
            with pytest.raises(LifecycleError, match="ownership is unresolved"):
                await rig.coordinator.reconcile_managed_runs()
            assert rig.store.get_run(run.id).state is run.state
            assert rig.store.find_active_reservation(rig.job.id).id == reservation.id
            assert rig.store.find_active_allocation(rig.job.id).id == allocation.id
            assert rig.provider.starts == len(rig.store.list_runs(rig.job.id)) == 1
            assert await rig.plane.dispatch_next() is None

    asyncio.run(scenario())


def test_progress_cursor_without_new_usage_does_not_double_charge(tmp_path):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            rig.quota()
            run = await rig.plane.dispatch_next()
            await rig.coordinator.reconcile_managed_runs()
            initial = rig.provider.observe(run.id)
            rig.provider.observe = lambda run_id: replace(initial, cursor="progress-13")
            await rig.coordinator.reconcile_managed_runs()
            assert (
                rig.store.get_driver_session(run.id).observation_cursor == "progress-13"
            )
            assert rig.store.get_quota_pool("account").remaining == 988
            assert len(rig.store.list_usage_samples(run.id)) == 1
            await rig.reopen_controller()
            await rig.coordinator.reconcile_managed_runs()
            assert rig.store.get_quota_pool("account").remaining == 988

    asyncio.run(scenario())


@pytest.mark.parametrize("already_persisted", [False, True])
def test_worker_terminal_result_recovers_without_live_observation_handle(
    tmp_path, already_persisted
):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            rig.quota()
            run = await rig.plane.dispatch_next()
            await rig.coordinator.reconcile_managed_runs()
            rig.provider.finished.set()
            terminal = await rig.worker.collect(rig.worker._runs[run.id].handle)
            terminal = replace(
                terminal,
                consumed_quota=0,
                usage=TokenUsage(input_tokens=30, output_tokens=5),
                metadata={"telemetry_valid": True},
            )
            rig.worker._write(
                rig.worker._lease(run.id) / "result.json", terminal.to_dict()
            )
            if already_persisted:
                current = rig.store.get_run(run.id)
                rig.store.save_run(replace(current, result=terminal), expected=current)
            # Simulate the worker process losing all live handles. Its journal
            # and terminal result remain authoritative, as in a real restart.
            rig.server._service._runs.clear()
            rig.worker._runs.clear()
            await rig.reopen_controller()
            await rig.coordinator.reconcile_managed_runs()
            assert rig.store.get_job(rig.job.id).state is JobState.CANCELLED
            assert rig.store.get_run(run.id).result.consumed_quota == 35
            assert rig.store.get_quota_pool("account").remaining == 965
            assert rig.provider.starts == len(rig.store.list_runs(rig.job.id)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["reserve", "future", "stale", "fetch_failed"])
def test_slow_quota_poll_preserves_healthy_run_but_still_enforces_reserve(
    tmp_path, stop
):
    async def scenario():
        now = [utc_now()]
        used = [9]
        skew = [timedelta()]
        failed = [False]
        async with coding_rig(
            tmp_path,
            account_policy=AccountPolicyThresholds(
                background_block_used_percent=90,
                urgent_only_used_percent=90,
                require_complete_windows=True,
            ),
            provider_stop_policy=ProviderStopPolicy(remaining_fraction=0.1),
        ) as rig:
            window = {
                "primary_window_minutes": 10080,
                "primary_reset_at": now[0] + timedelta(days=7),
                "metadata": {"reported_windows": ["primary"]},
            }
            rig.quota(9, **window)
            run = await rig.plane.dispatch_next()
            assert run is not None

            class SlowOracle:
                async def snapshot(self):
                    await asyncio.sleep(0)
                    now[0] += timedelta(seconds=20)
                    if failed[0]:
                        raise RuntimeError("quota fetch failed")
                    snapshot = ProviderQuotaSnapshot(
                        pool_id="account",
                        bucket_id="account",
                        primary_used_percent=used[0],
                        observed_at=now[0] + skew[0],
                        **window,
                    )
                    rig.store.append_provider_quota_snapshot(snapshot)
                    return snapshot

            daemon = AgentDaemon(
                rig.plane,
                account_oracle=SlowOracle(),
                account_poll_seconds=1,
                clock=lambda: now[0],
            )
            await daemon.tick()
            assert rig.provider.cancels == 0
            assert rig.store.get_run(run.id).state is RunState.RUNNING
            assert rig.store.list_run_commands(run.id) == []
            assert rig.store.get_quota_pool("account").remaining == 988

            now[0] += timedelta(seconds=1)
            if stop == "reserve":
                used[0] = 90
            elif stop == "future":
                skew[0] = timedelta(seconds=1)
            elif stop == "stale":
                skew[0] = -timedelta(minutes=6)
            else:
                failed[0] = True
                now[0] += timedelta(minutes=6)
            await daemon.tick()
            assert rig.provider.cancels >= 1
            assert any(
                command.action == "interrupt"
                for command in rig.store.list_run_commands(run.id)
            )
            assert rig.store.get_job(rig.job.id).state is JobState.SUSPENDED
            assert rig.store.get_quota_pool("account").remaining == 988
            assert rig.provider.starts == 1

    asyncio.run(scenario())


def test_uncached_run_checkpoint_preserves_usage_across_controller_restart(tmp_path):
    class CachedProvider(PausedProvider):
        def observe(self, run_id):
            observation = super().observe(run_id)
            usage = TokenUsage(
                input_tokens=1000, cached_input_tokens=995, output_tokens=7
            )
            return replace(
                observation,
                usage=usage,
                result=replace(observation.result, usage=usage)
                if observation.result is not None
                else None,
                metadata={"quota_basis": "uncached-v1"},
            )

    async def scenario():
        async with coding_rig(
            tmp_path,
            provider=CachedProvider(),
            quota_basis="uncached-v1",
            maximum_run_quota=13,
        ) as rig:
            rig.quota()
            run = await rig.plane.dispatch_next()
            assert run is not None
            await rig.coordinator.reconcile_managed_runs()
            assert rig.store.get_job(rig.job.id).state is JobState.SUSPENDED
            assert rig.store.get_quota_pool("account").remaining == 988
            assert rig.store.get_run(run.id).result.usage.total_tokens == 1007
            commands = rig.store.list_run_commands(run.id)
            assert any(command.action == "checkpoint" for command in commands)
            await rig.reopen_controller()
            await rig.coordinator.recover_managed_runs()
            await rig.coordinator.resume(rig.job.id)
            rig.provider.finished = asyncio.Event()
            await rig.plane.refresh_worker_heartbeats()
            resumed = await rig.plane.dispatch_next()
            order = resumed.contract.operation.work_order
            assert order.prior_consumed_quota == 12
            assert order.maximum_run_quota == 13
            assert order.quota_basis == "uncached-v1"
            assert rig.store.get_quota_pool("account").remaining == 988

    asyncio.run(scenario())


def test_budget_reset_retains_history_and_starts_fresh_bounded_attempts(tmp_path):
    from agentd.coding.attempts import (
        CodingAttemptLimits,
        current_attempts,
        current_consumed,
    )
    from agentd.coding.controller import guard_coding_resume

    async def scenario():
        async with coding_rig(tmp_path) as rig:
            rig.quota()
            old_run = await rig.plane.dispatch_next()
            assert old_run is not None
            arguments = dict(
                expected_run_id=old_run.id,
                actor="operator",
                maximum_tokens=900,
                maximum_run_tokens=300,
            )
            with pytest.raises(LifecycleError, match="stopped"):
                await rig.coordinator.reset_coding_budget(rig.job.id, **arguments)
            await rig.worker.cancel(rig.worker._runs[old_run.id].handle)
            await rig.coordinator.reconcile_managed_runs()
            original = rig.store.get_run(old_run.id)
            assert rig.store.get_quota_pool("account").remaining == 988
            reset = await rig.coordinator.reset_coding_budget(rig.job.id, **arguments)
            assert reset.state is JobState.READY
            assert rig.store.get_run(old_run.id) == original
            assert current_consumed(reset, rig.store.list_reservations(reset.id)) == 0
            assert current_attempts(reset, rig.store.list_runs(reset.id)) == []
            assert (
                await rig.coordinator.reset_coding_budget(reset.id, **arguments)
                == reset
            )
            await rig.reopen_controller()
            for attempt in range(3):
                rig.provider.finished = asyncio.Event()
                await rig.plane.refresh_worker_heartbeats()
                run = await rig.plane.dispatch_next()
                assert run is not None
                order = run.contract.operation.work_order
                assert order.prior_consumed_quota == attempt * 12
                assert order.maximum_quota == 900
                assert order.maximum_run_quota == 300
                assert order.quota_basis == "uncached-v1"
                # A repeated old reset must not grant fresh credit to a live run.
                assert (
                    await rig.coordinator.reset_coding_budget(reset.id, **arguments)
                ).state is JobState.RUNNING
                await rig.worker.interrupt(rig.worker._runs[run.id].handle)
                await rig.coordinator.reconcile_managed_runs()
                assert rig.store.get_job(reset.id).state is JobState.SUSPENDED
                if attempt < 2:
                    guard_coding_resume(
                        rig.store, reset.id, CodingAttemptLimits(3, 3, 3)
                    )
                    await rig.coordinator.resume(reset.id)
            with pytest.raises(LifecycleError, match="attempt limit"):
                guard_coding_resume(rig.store, reset.id, CodingAttemptLimits(3, 3, 3))
            latest = rig.store.get_job(reset.id)
            assert current_consumed(latest, rig.store.list_reservations(reset.id)) == 36
            assert rig.store.get_quota_pool("account").remaining == 952
            assert len(rig.store.list_runs(reset.id)) == 4
            assert rig.store.get_run(old_run.id) == original

    asyncio.run(scenario())


def test_unstarted_budget_configuration_cannot_change_started_work(tmp_path):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            args = dict(
                expected_run_id=None,
                actor="operator",
                maximum_tokens=900,
                maximum_run_tokens=300,
            )
            configured = await rig.coordinator.reset_coding_budget(rig.job.id, **args)
            assert configured.operation.work_order.quota_basis == "uncached-v1"
            assert configured.quota_budget.maximum == 900
            rig.quota()
            run = await rig.plane.dispatch_next()
            assert run.contract.operation.work_order.maximum_run_quota == 300
            with pytest.raises(LifecycleError, match="without runs"):
                await rig.coordinator.reset_coding_budget(
                    rig.job.id, **{**args, "maximum_tokens": 901}
                )

    asyncio.run(scenario())


def test_admin_queue_replays_reset_after_lost_ack_without_new_allowance(tmp_path):
    from agentd.coding.admin import CodingAdminStore

    async def scenario():
        async with coding_rig(tmp_path) as rig:
            queue = CodingAdminStore(rig.store.path)
            payload = dict(
                job_id=rig.job.id,
                expected_run_id=None,
                actor="operator",
                maximum_tokens=900,
                maximum_run_tokens=300,
            )
            request = queue.enqueue(payload)
            # Reset committed, then the process lost its request acknowledgement.
            await rig.coordinator.reset_coding_budget(**payload)
            rig.quota()
            run = await rig.plane.dispatch_next()
            assert run is not None
            await queue.apply_pending(rig.coordinator)
            assert queue.get(request["request_id"])["result"]["ok"] is True
            assert len(rig.store.list_runs(rig.job.id)) == 1
            assert rig.store.get_job(rig.job.id).state is JobState.RUNNING
            assert (
                rig.store.get_job(rig.job.id).operation.work_order.retired_run_ids == ()
            )
            bad = queue.enqueue({**payload, "maximum_tokens": 901})
            await queue.apply_pending(rig.coordinator)
            assert queue.get(bad["request_id"])["result"]["ok"] is False
            assert rig.store.get_job(rig.job.id).quota_budget.maximum == 900

    asyncio.run(scenario())
