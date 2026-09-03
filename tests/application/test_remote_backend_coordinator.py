from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import (
    ArtifactKind,
    JobState,
    RunOutcome,
    WorkspaceState,
)
from agentd.domain.models import (
    ArtifactRecord,
    ArtifactRef,
    ArtifactSpec,
    BuildImageOperation,
    ProducedArtifact,
    QuotaPool,
    ResourceVector,
    RunCommand,
    RunHandle,
    RunObservation,
    RunResult,
    WorkerNode,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers import (
    ARTIFACT_VERIFICATION_FEATURE,
    BackendRegistry,
    WorkerBackendCapabilities,
    WorkerStartUncertainError,
)

GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 40)
IMAGE = ArtifactRef(
    ArtifactKind.OCI_IMAGE,
    "ghcr.io/acme/app@sha256:" + "b" * 64,
)


class ExplodingWorkspaceManager:
    """A typed operation must never ask the controller for a local workspace."""

    def allocate(self, *_args, **_kwargs):
        raise AssertionError("remote typed operation allocated a local workspace")

    def release(self, *_args, **_kwargs):
        raise AssertionError("remote typed operation released through local manager")

    def is_available(self, *_args, **_kwargs):
        raise AssertionError("remote typed operation inspected a local workspace")

    def current_commit(self, *_args, **_kwargs):
        raise AssertionError("remote typed operation inspected a local commit")

    def commit_changes(self, *_args, **_kwargs):
        raise AssertionError("remote typed operation committed locally")


class ExplodingHarness(FakeHarnessDriver):
    async def start(self, _execution):
        raise AssertionError("remote backend fell back to local start")

    async def steer(self, _run, _instruction):
        raise AssertionError("remote backend fell back to local steer")

    async def interrupt(self, _run):
        raise AssertionError("remote backend fell back to local interrupt")

    async def collect(self, _run):
        raise AssertionError("remote backend fell back to local collect")

    async def cancel(self, _run):
        raise AssertionError("remote backend fell back to local cancel")


class ManagedExplodingHarness(ExplodingHarness):
    async def start_managed(self, _run_id, _execution):
        raise AssertionError("remote backend fell back to local managed start")

    async def recover(self, _run_id, _execution, _recovery_instruction="Resume"):
        raise AssertionError("remote backend fell back to local recovery")

    def observe(self, _run_id):
        raise AssertionError("remote backend fell back to local observe")

    async def continue_turn(self, _run_id, _instruction):
        raise AssertionError("remote backend fell back to local continuation")


class FakeRemoteBackend:
    def __init__(self, result: RunResult | None = None) -> None:
        self._result = result or RunResult(
            RunOutcome.COMPLETED,
            "remote build complete",
            consumed_quota=1,
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        )
        self.dispatches: list[tuple[object, str | None, bool]] = []
        self.collects: list[RunHandle | str] = []
        self.cancels: list[RunHandle | str] = []
        self.steers: list[tuple[RunHandle | str, str]] = []
        self.interrupts: list[RunHandle | str] = []
        self.observations: list[str] = []
        self.observation: RunObservation | None = None
        self.status_targets: list[RunHandle | str] = []
        self.status_payload: dict[str, object] = {
            "known": True,
            "terminal": False,
            "result": None,
        }

    def capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            name="remote-test",
            features=frozenset(
                {
                    ARTIFACT_VERIFICATION_FEATURE,
                    "build-image",
                    "deploy-image",
                }
            ),
            remote=True,
        )

    def is_compatible(self, node: WorkerNode) -> bool:
        return node.id == "remote-node" and node.labels.get("backend") == "remote"

    async def dispatch(self, driver, contract, *, run_id=None, managed=False):
        self.dispatches.append((contract, run_id, managed))
        return RunHandle(
            id=f"remote-handle-{len(self.dispatches)}",
            driver=driver.capabilities().name,
            external_id=f"remote-external-{len(self.dispatches)}",
        )

    async def observe(self, run_id: str):
        self.observations.append(run_id)
        return self.observation

    async def status(self, run: RunHandle | str):
        self.status_targets.append(run)
        return dict(self.status_payload)

    async def steer(self, run: RunHandle | str, instruction: str) -> None:
        self.steers.append((run, instruction))

    async def interrupt(self, run: RunHandle | str) -> None:
        self.interrupts.append(run)

    async def cancel(self, run: RunHandle | str) -> None:
        self.cancels.append(run)

    async def collect(self, run: RunHandle | str) -> RunResult:
        self.collects.append(run)
        return self._result

    async def heartbeat(self) -> dict[str, object]:
        return {"backend": "remote-test", "node": "remote-node"}

    async def close(self) -> None:
        return None


def _external(ref: ArtifactRef) -> ArtifactRecord:
    return ArtifactRecord(
        id="external-source",
        ref=ref,
        producer_job_id=None,
        producer_run_id=None,
        spec_name="source",
        verified=True,
        verified_at=datetime.now(UTC),
        external=True,
    )


def _remote_node() -> WorkerNode:
    return WorkerNode(
        id="remote-node",
        labels={"os": "linux", "arch": "x86_64", "backend": "remote"},
        capacity=ResourceVector(cpu=8, ram_gb=32),
        harnesses=frozenset({"fake"}),
    )


def _build_job(make_application_job, *, job_id: str = "build-job"):
    return make_application_job(
        id=job_id,
        artifact_inputs=(GIT,),
        artifact_outputs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
        operation=BuildImageOperation(
            source_input=GIT,
            source_repository="https://github.com/acme/app.git",
            output_name="image",
            registry_repository="ghcr.io/acme/app",
        ),
    )


def _setup(
    make_application_job,
    *,
    driver=None,
    backend=None,
    path: Path | None = None,
):
    store = SQLiteStateStore(path) if path is not None else SQLiteStateStore()
    remote = backend or FakeRemoteBackend()
    harness = driver or ExplodingHarness()
    coordinator = SchedulerCoordinator(
        store,
        ExplodingWorkspaceManager(),
        DriverRegistry((harness,)),
        backends=BackendRegistry((remote,)),
    )
    plane = ControlPlane(store, coordinator=coordinator)
    plane.register_node(_remote_node())
    plane.register_quota_pool(QuotaPool(id="default", provider="remote", remaining=100))
    store.register_external_artifact(_external(GIT))
    job = _build_job(make_application_job)
    plane.submit(job)
    return store, coordinator, plane, remote, harness, job


def test_remote_typed_build_uses_contract_and_releases_logical_capacity(
    make_application_job,
):
    store, _coordinator, plane, backend, _harness, job = _setup(make_application_job)

    run = asyncio.run(plane.dispatch_next())
    assert run is not None
    contract, dispatched_run_id, managed = backend.dispatches[0]
    assert dispatched_run_id == run.id
    assert managed is False
    assert contract.operation == job.operation
    assert contract.artifact_inputs == (GIT,)
    assert contract.allowed_filesystem_scope == ()
    assert contract.working_directory == "worker://remote-node/build-job"
    assert store.find_active_allocation(job.id) is not None
    assert asyncio.run(plane.complete(job.id)).state is JobState.COMPLETED
    assert backend.collects == [run.id]
    assert store.list_artifacts(job_id=job.id)[0].ref == IMAGE
    workspace = store.find_workspace(job.id)
    assert workspace is not None and workspace.state is WorkspaceState.RELEASED
    assert store.find_active_allocation(job.id) is None
    assert store.find_active_reservation(job.id) is None
    store.close()


def test_cancel_routes_remote_and_retries_without_second_remote_effect(
    make_application_job,
):
    store, _coordinator, plane, backend, _harness, job = _setup(make_application_job)
    run = asyncio.run(plane.dispatch_next())
    assert run is not None

    assert asyncio.run(plane.cancel(job.id)).state is JobState.CANCELLED
    assert backend.cancels == [run.id]
    assert backend.collects == [run.id]
    assert asyncio.run(plane.cancel(job.id)).state is JobState.CANCELLED
    assert backend.cancels == [run.id]
    assert backend.collects == [run.id]
    assert store.find_active_allocation(job.id) is None
    assert store.find_active_reservation(job.id) is None
    store.close()


def test_dispatch_failure_compensates_through_remote_cancel_and_collect(
    make_application_job,
    monkeypatch,
):
    store, _coordinator, plane, backend, _harness, job = _setup(make_application_job)
    original = store.save_run
    calls = 0

    def fail_after_handle(run, *, expected=None):
        nonlocal calls
        calls += 1
        if expected is not None:
            raise RuntimeError("simulated running publication failure")
        return original(run, expected=expected)

    monkeypatch.setattr(store, "save_run", fail_after_handle)
    with pytest.raises(RuntimeError, match="publication"):
        asyncio.run(plane.dispatch_next())
    assert calls == 2
    failed_run = store.list_runs(job.id)[0]
    assert backend.cancels == [failed_run.id]
    assert backend.collects == [failed_run.id]
    assert store.get_job(job.id).state is JobState.READY
    assert store.find_active_allocation(job.id) is None
    assert store.find_active_reservation(job.id) is None
    store.close()


class UncertainStartBackend(FakeRemoteBackend):
    """Simulate a worker side effect whose START response was lost."""

    def __init__(self, *, cancellation: bool = False) -> None:
        super().__init__()
        self._cancellation = cancellation
        self._uncertain = True

    async def dispatch(self, driver, contract, *, run_id=None, managed=False):
        self.dispatches.append((contract, run_id, managed))
        if self._uncertain:
            self._uncertain = False
            if self._cancellation:
                raise asyncio.CancelledError
            raise WorkerStartUncertainError("simulated lost START response")
        return await super().dispatch(
            driver,
            contract,
            run_id=run_id,
            managed=managed,
        )


def test_uncertain_remote_start_is_reconciled_without_release_or_redispatch(
    make_application_job,
):
    store, coordinator, plane, backend, _harness, job = _setup(
        make_application_job,
        backend=UncertainStartBackend(),
    )
    try:
        with pytest.raises(WorkerStartUncertainError):
            asyncio.run(plane.dispatch_next())

        run = store.list_runs(job.id)[0]
        assert store.get_job(job.id).state is JobState.ADMITTED
        assert run.state.value == "STARTING"
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
        assert backend.cancels == []
        assert backend.collects == []

        assert asyncio.run(coordinator.reconcile_managed_runs()) == ()
        assert backend.status_targets == [run.id]
        assert store.get_job(job.id).state is JobState.RUNNING
        assert store.get_run(run.id).state.value == "RUNNING"
        assert len(backend.dispatches) == 1
    finally:
        store.close()


def test_authoritative_unknown_remote_start_fails_and_cleans_admission(
    make_application_job,
):
    backend = UncertainStartBackend()
    store, coordinator, plane, _backend, _harness, job = _setup(
        make_application_job,
        backend=backend,
    )
    try:
        with pytest.raises(WorkerStartUncertainError):
            asyncio.run(plane.dispatch_next())
        backend.status_payload = {
            "known": False,
            "terminal": False,
            "result": None,
        }

        assert asyncio.run(coordinator.reconcile_managed_runs()) == ()
        assert store.get_job(job.id).state is JobState.FAILED
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
        assert backend.cancels == []
        assert backend.collects == []
    finally:
        store.close()


def test_unknown_remote_start_retries_failed_cleanup_without_redispatch(
    make_application_job,
    monkeypatch,
):
    backend = UncertainStartBackend()
    store, coordinator, plane, _backend, _harness, job = _setup(
        make_application_job,
        backend=backend,
    )
    cleanup_calls = 0
    original_cleanup = coordinator._cleanup_job_resources

    def fail_cleanup_once(*args, **kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            return [RuntimeError("injected terminal cleanup failure")]
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_cleanup_job_resources", fail_cleanup_once)
    try:
        with pytest.raises(WorkerStartUncertainError):
            asyncio.run(plane.dispatch_next())
        backend.status_payload = {
            "known": False,
            "terminal": False,
            "result": None,
        }

        with pytest.raises(RuntimeError, match="terminal cleanup"):
            asyncio.run(coordinator.reconcile_managed_runs())
        assert store.get_job(job.id).state is JobState.FAILED
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
        assert len(backend.dispatches) == 1

        assert asyncio.run(coordinator.reconcile_managed_runs()) == ()
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
        assert cleanup_calls == 2
        assert len(backend.dispatches) == 1
        assert backend.cancels == []
        assert backend.collects == []
    finally:
        store.close()


def test_cancelled_remote_start_keeps_same_starting_intent(
    make_application_job,
):
    store, _coordinator, plane, backend, _harness, job = _setup(
        make_application_job,
        backend=UncertainStartBackend(cancellation=True),
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(plane.dispatch_next())
        run = store.list_runs(job.id)[0]
        assert store.get_job(job.id).state is JobState.ADMITTED
        assert run.state.value == "STARTING"
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
        assert backend.cancels == []
        assert backend.collects == []
    finally:
        store.close()


def test_managed_remote_reconcile_routes_observation_usage_and_commands(
    make_application_job,
):
    backend = FakeRemoteBackend()
    driver = ManagedExplodingHarness()
    store, _coordinator, plane, _backend, _harness, _job = _setup(
        make_application_job,
        driver=driver,
        backend=backend,
    )
    run = asyncio.run(plane.dispatch_next())
    assert run is not None
    assert backend.dispatches[0][2] is True
    backend.observation = RunObservation(
        run_id=run.id,
        thread_id="remote-thread",
        turn_id="turn-1",
        cursor="cursor-1",
        cumulative_quota=1,
        source="remote-test",
    )
    steer = RunCommand(run_id=run.id, action="steer", payload={"instruction": "hold"})
    interrupt = RunCommand(run_id=run.id, action="interrupt")
    store.enqueue_run_command(steer)
    store.enqueue_run_command(interrupt)

    assert asyncio.run(plane.reconcile_managed_runs()) == ()
    assert backend.observations == [run.id]
    assert len(store.list_usage_samples(run.id)) == 1
    assert len(backend.steers) == 1
    assert backend.steers[0][0] == run.id
    assert f"Control-plane command id: {steer.id}" in backend.steers[0][1]
    assert backend.interrupts == [run.id]
    assert store.list_pending_run_commands(run.id) == []
    store.close()


def test_remote_actions_use_durable_run_id_after_controller_restart(
    tmp_path: Path,
    make_application_job,
):
    path = tmp_path / "remote-action-restart.sqlite"
    backend = FakeRemoteBackend()
    driver = ManagedExplodingHarness()
    first_store, _coordinator, first_plane, _backend, _harness, job = _setup(
        make_application_job,
        driver=driver,
        backend=backend,
        path=path,
    )
    try:
        run = asyncio.run(first_plane.dispatch_next())
        assert run is not None
    finally:
        first_store.close()

    reopened = SQLiteStateStore(path)
    restarted = SchedulerCoordinator(
        reopened,
        ExplodingWorkspaceManager(),
        DriverRegistry((driver,)),
        backends=BackendRegistry((backend,)),
    )
    plane = ControlPlane(reopened, coordinator=restarted)
    backend.observation = RunObservation(
        run_id=run.id,
        thread_id="remote-thread",
        turn_id="turn-restart",
        cursor="cursor-restart",
        cumulative_quota=1,
        source="remote-test",
    )
    steer = RunCommand(
        run_id=run.id,
        action="steer",
        payload={"instruction": "continue after restart"},
    )
    interrupt = RunCommand(run_id=run.id, action="interrupt")
    reopened.enqueue_run_command(steer)
    reopened.enqueue_run_command(interrupt)

    try:
        assert asyncio.run(plane.reconcile_managed_runs()) == ()
        assert backend.observations[-1] == run.id
        assert backend.steers[-1][0] == run.id
        assert backend.interrupts[-1] == run.id
        assert reopened.list_pending_run_commands(run.id) == []

        assert asyncio.run(plane.cancel(job.id)).state is JobState.CANCELLED
        assert backend.cancels[-1] == run.id
        assert backend.collects[-1] == run.id
    finally:
        reopened.close()


def test_missing_remote_backend_after_restart_fails_closed_without_local_fallback(
    tmp_path: Path,
    make_application_job,
):
    path = tmp_path / "remote-restart.sqlite"
    first_store, _coordinator, _first_plane, _backend, _harness, job = _setup(
        make_application_job,
        path=path,
    )
    first_store.close()

    reopened = SQLiteStateStore(path)
    local = ExplodingHarness()
    coordinator = SchedulerCoordinator(
        reopened,
        ExplodingWorkspaceManager(),
        DriverRegistry((local,)),
        backends=BackendRegistry(()),
    )
    plane = ControlPlane(reopened, coordinator=coordinator)
    assert asyncio.run(plane.dispatch_next()) is None
    assert reopened.get_job(job.id).state is JobState.READY
    assert reopened.list_reservations(job.id) == []
    assert reopened.list_allocations(job.id) == []
    assert reopened.list_workspaces(job.id) == []
    reopened.close()


def test_heartbeat_binds_backend_and_preserves_declared_capacity() -> None:
    class HeartbeatBackend(FakeRemoteBackend):
        def capabilities(self) -> WorkerBackendCapabilities:
            return WorkerBackendCapabilities(name="remote-heartbeat", remote=True)

        async def heartbeat(self) -> dict[str, object]:
            return {
                "node_id": "heartbeat-node",
                "session_epoch": "epoch-1",
                "drivers": ["operations"],
                "active_runs": 1,
            }

    store = SQLiteStateStore()
    original_time = datetime(2026, 8, 9, tzinfo=UTC)
    node = WorkerNode(
        id="heartbeat-node",
        labels={"os": "linux", "arch": "x86_64"},
        capacity=ResourceVector(cpu=8, ram_gb=32),
        allocated=ResourceVector(cpu=2, ram_gb=4),
        harnesses=frozenset({"operations"}),
        updated_at=original_time,
    )
    store.register_node(node)
    coordinator = SchedulerCoordinator(
        store,
        ExplodingWorkspaceManager(),
        DriverRegistry(),
        backends=BackendRegistry((HeartbeatBackend(),)),
    )

    snapshots = asyncio.run(coordinator.refresh_worker_heartbeats())

    assert snapshots[0]["active_runs"] == 1
    refreshed = store.get_node(node.id)
    assert refreshed.labels["backend"] == "remote-heartbeat"
    assert refreshed.capacity == node.capacity
    assert refreshed.allocated == node.allocated
    assert refreshed.updated_at > original_time
    assert refreshed.heartbeat is not None
    assert refreshed.heartbeat.session_epoch == "epoch-1"
    assert refreshed.heartbeat.drivers == frozenset({"operations"})
    assert refreshed.heartbeat.active_runs == 1
    store.close()


def test_heartbeat_cannot_rebind_a_node_to_another_backend() -> None:
    class WrongBackend(FakeRemoteBackend):
        def capabilities(self) -> WorkerBackendCapabilities:
            return WorkerBackendCapabilities(name="remote-other", remote=True)

        async def heartbeat(self) -> dict[str, object]:
            return {"node_id": "bound-node", "session_epoch": "epoch-1"}

    store = SQLiteStateStore()
    store.register_node(
        WorkerNode(
            id="bound-node",
            labels={"backend": "remote-original"},
            capacity=ResourceVector(cpu=2, ram_gb=4),
            harnesses=frozenset({"operations"}),
        )
    )
    coordinator = SchedulerCoordinator(
        store,
        ExplodingWorkspaceManager(),
        DriverRegistry(),
        backends=BackendRegistry((WrongBackend(),)),
    )

    with pytest.raises(ExceptionGroup, match="heartbeats failed"):
        asyncio.run(coordinator.refresh_worker_heartbeats())

    assert store.get_node("bound-node").labels["backend"] == "remote-original"
    store.close()
