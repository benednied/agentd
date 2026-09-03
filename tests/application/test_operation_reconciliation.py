from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentd.coordinator import LifecycleError, SchedulerCoordinator
from agentd.daemon import AgentDaemon
from agentd.domain.enums import (
    ArtifactKind,
    JobState,
    RunOutcome,
    RunState,
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
    RunHandle,
    RunRecord,
    RunResult,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import transition_job
from agentd.harness.registry import DriverRegistry
from agentd.scheduling.placement import Placement
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.controller import OperationsHarnessDescriptor
from agentd.workers.errors import (
    WorkerOperationError,
    WorkerProtocolError,
    WorkerTransportError,
)
from agentd.workers.protocol import (
    ARTIFACT_VERIFICATION_FEATURE,
    WorkerBackendCapabilities,
)
from agentd.workers.registry import BackendRegistry

GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 40)
IMAGE = ArtifactRef(
    ArtifactKind.OCI_IMAGE,
    "ghcr.io/acme/app@sha256:" + "b" * 64,
)


class TypedWorkspaceManager:
    def allocate(self, job, base_ref="HEAD") -> WorkspaceLease:
        return WorkspaceLease(
            id=f"workspace-{job.id}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
        )

    def release(self, workspace: WorkspaceLease) -> WorkspaceLease:
        return replace(workspace, state=WorkspaceState.RELEASED)

    def is_available(self, workspace: WorkspaceLease) -> bool:
        return False

    def current_commit(self, workspace: WorkspaceLease) -> str:
        return "f" * 40

    def commit_changes(self, workspace: WorkspaceLease) -> str:
        return self.current_commit(workspace)


class FakeTypedBackend:
    def __init__(self, *, verifies_artifacts: bool = True) -> None:
        self.verifies_artifacts = verifies_artifacts
        self.status_payload: dict[str, object] = {
            "known": True,
            "terminal": False,
            "result": None,
        }
        self.handle = RunHandle(id="remote-handle", driver="operations")
        self.dispatches = 0
        self.status_calls = 0
        self.collect_calls = 0
        self.collect_error: BaseException | None = None
        self.status_targets: list[RunHandle | str] = []
        self.collect_targets: list[RunHandle | str] = []

    def capabilities(self) -> WorkerBackendCapabilities:
        features = {"build-image", "deploy-image"}
        if self.verifies_artifacts:
            features.add(ARTIFACT_VERIFICATION_FEATURE)
        return WorkerBackendCapabilities(
            name="remote-a",
            features=frozenset(features),
            remote=True,
        )

    def is_compatible(self, node: WorkerNode) -> bool:
        return node.labels.get("backend") == "remote-a" and node.id == "node-a"

    async def dispatch(self, driver, contract, *, run_id=None, managed=False):
        del driver, contract, run_id, managed
        self.dispatches += 1
        return self.handle

    async def status(self, run):
        self.status_targets.append(run)
        self.status_calls += 1
        return dict(self.status_payload)

    async def collect(self, run):
        self.collect_targets.append(run)
        self.collect_calls += 1
        if self.collect_error is not None:
            raise self.collect_error
        raw = self.status_payload.get("result")
        if not isinstance(raw, dict):
            raise RuntimeError("test backend has no terminal result")
        return RunResult.from_dict(raw)

    async def observe(self, run_id):
        del run_id
        return None

    async def steer(self, run, instruction):
        del run, instruction

    async def interrupt(self, run):
        del run

    async def cancel(self, run):
        del run

    async def heartbeat(self):
        return {
            "node_id": "node-a",
            "session_epoch": "epoch-a",
            "drivers": ["operations"],
            "active_runs": 0,
        }

    async def close(self):
        return None


def _build_job(make_application_job, *, job_id: str = "build-job"):
    return make_application_job(
        id=job_id,
        artifact_inputs=(GIT,),
        artifact_outputs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
        preferred_harnesses=("operations",),
        allowed_harnesses=("operations",),
        operation=BuildImageOperation(
            source_input=GIT,
            source_repository="https://github.com/acme/app.git",
            output_name="image",
            registry_repository="ghcr.io/acme/app",
        ),
    )


def _external() -> ArtifactRecord:
    now = datetime.now(UTC)
    return ArtifactRecord(
        id="external-source",
        ref=GIT,
        producer_job_id=None,
        producer_run_id=None,
        spec_name="source",
        verified=True,
        verified_at=now,
        created_at=now,
        external=True,
    )


def _setup(
    make_application_job,
    *,
    verifies_artifacts: bool = True,
    path: Path | None = None,
):
    store = SQLiteStateStore(path) if path is not None else SQLiteStateStore()
    backend = FakeTypedBackend(verifies_artifacts=verifies_artifacts)
    backends = BackendRegistry((backend,))
    coordinator = SchedulerCoordinator(
        store,
        TypedWorkspaceManager(),
        DriverRegistry((OperationsHarnessDescriptor(),)),
        backends=backends,
    )
    plane = ControlPlane(store, coordinator=coordinator)
    plane.register_node(
        WorkerNode(
            id="node-a",
            labels={"backend": "remote-a"},
            capacity=ResourceVector(cpu=8, ram_gb=32),
            harnesses=frozenset({"operations"}),
        )
    )
    plane.register_quota_pool(QuotaPool(id="default", provider="worker", remaining=100))
    store.register_external_artifact(_external())
    job = _build_job(make_application_job)
    plane.submit(job)
    return store, plane, backend, backends, job


def _seed_starting_operation_intent(
    store: SQLiteStateStore,
    plane: ControlPlane,
    backend: FakeTypedBackend,
    job,
) -> RunRecord:
    """Persist the exact boundary immediately before remote START returns."""

    coordinator = plane._coordinator
    assert coordinator is not None
    job = store.get_job(job.id)
    placement = Placement(
        node_id="node-a",
        harness="operations",
        model_class="standard",
        harness_preference=0,
        resource_waste=0,
    )
    reservation = coordinator._quota.reserve(job)
    allocation = coordinator._resources.allocate(job, store.get_node("node-a"))
    workspace = coordinator._operation_workspace(job, placement, backend)
    store.save_workspace(workspace, expected=None)
    selected = replace(
        job,
        selected_harness="operations",
        selected_model_class="standard",
    )
    admitted, event = transition_job(
        selected,
        JobState.ADMITTED,
        "seed crash boundary before typed START response",
    )
    store.save_job(admitted, event, expected=job)
    resolved_inputs, resolved_operation = coordinator._resolve_artifacts(admitted)
    contract = coordinator._build_contract(
        admitted,
        workspace,
        artifact_inputs=resolved_inputs,
        operation=resolved_operation,
    )
    run = RunRecord(
        job_id=job.id,
        node_id="node-a",
        workspace_id=workspace.id,
        reservation_id=reservation.id,
        allocation_id=allocation.id,
        driver="operations",
        backend="remote-a",
        contract=contract,
        handle=RunHandle(id="pending-crash", driver="operations"),
        state=RunState.STARTING,
    )
    store.save_run(run, expected=None)
    return run


def test_reconcile_terminal_build_publishes_and_cleans_resources(make_application_job):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        backend.status_payload = {
            "known": True,
            "terminal": True,
            "result": RunResult(
                RunOutcome.COMPLETED,
                "image built",
                consumed_quota=1,
                produced_artifacts=(ProducedArtifact("image", IMAGE),),
            ).to_dict(),
        }
        finalized = asyncio.run(plane.reconcile_managed_runs())
        assert backend.status_targets == [run.id]
        assert [item.id for item in finalized] == [job.id]
        assert plane.inspect_job(job.id).state is JobState.COMPLETED
        assert store.get_run(run.id).state is RunState.COMPLETED
        assert store.list_artifacts(job_id=job.id)[0].ref == IMAGE
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
    finally:
        store.close()


def test_reconcile_terminal_collect_failure_becomes_failed_and_cleans_resources(
    make_application_job,
):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        backend.status_payload = {"known": True, "terminal": True, "result": None}
        finalized = asyncio.run(plane.reconcile_managed_runs())
        assert [item.id for item in finalized] == [job.id]
        assert plane.inspect_job(job.id).state is JobState.FAILED
        assert store.get_run(run.id).result is not None
        assert store.get_run(run.id).result.outcome is RunOutcome.FAILED
        assert backend.status_targets == [run.id]
        assert backend.collect_targets == [run.id]
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
    finally:
        store.close()


def test_reconcile_terminal_collect_transport_failure_remains_retryable(
    make_application_job,
):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        backend.status_payload = {"known": True, "terminal": True, "result": None}
        backend.collect_error = WorkerTransportError("connection dropped")

        with pytest.raises(WorkerTransportError, match="connection dropped"):
            asyncio.run(plane.reconcile_managed_runs())

        assert plane.inspect_job(job.id).state is JobState.RUNNING
        assert store.get_run(run.id).state is RunState.RUNNING
        assert store.get_run(run.id).result is None
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None

        backend.collect_error = None
        backend.status_payload = {
            "known": True,
            "terminal": True,
            "result": RunResult(
                RunOutcome.COMPLETED,
                "image built after retry",
                consumed_quota=1,
                produced_artifacts=(ProducedArtifact("image", IMAGE),),
            ).to_dict(),
        }
        finalized = asyncio.run(plane.reconcile_managed_runs())
        assert [item.id for item in finalized] == [job.id]
        assert plane.inspect_job(job.id).state is JobState.COMPLETED
        assert store.get_run(run.id).result is not None
        assert store.get_run(run.id).result.outcome is RunOutcome.COMPLETED
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "collection_error",
    [
        WorkerOperationError("worker operation failed"),
        WorkerProtocolError("bad result"),
    ],
)
def test_reconcile_terminal_non_transport_collection_errors_fail_closed(
    make_application_job,
    collection_error,
):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        backend.status_payload = {"known": True, "terminal": True, "result": None}
        backend.collect_error = collection_error

        finalized = asyncio.run(plane.reconcile_managed_runs())

        assert [item.id for item in finalized] == [job.id]
        assert plane.inspect_job(job.id).state is JobState.FAILED
        stored = store.get_run(run.id)
        assert stored.state is RunState.FAILED
        assert stored.result is not None
        assert stored.result.outcome is RunOutcome.FAILED
    finally:
        store.close()


def test_reconcile_replays_durable_result_after_crash_before_terminal_publish(
    tmp_path,
    make_application_job,
    monkeypatch,
):
    path = tmp_path / "typed-result-crash.sqlite"
    store, plane, backend, backends, job = _setup(make_application_job, path=path)
    coordinator = plane._coordinator
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        terminal_result = RunResult(
            RunOutcome.COMPLETED,
            "image built",
            consumed_quota=1,
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        )
        backend.status_payload = {
            "known": True,
            "terminal": True,
            "result": terminal_result.to_dict(),
        }

        async def crash_before_publish(_job_id):
            raise RuntimeError("simulated controller crash")

        monkeypatch.setattr(coordinator, "complete", crash_before_publish)
        with pytest.raises(RuntimeError, match="simulated controller crash"):
            asyncio.run(plane.reconcile_managed_runs())

        persisted = store.get_run(run.id)
        assert persisted.result == terminal_result
        assert persisted.state is RunState.RUNNING
        status_calls_before_restart = backend.status_calls
    finally:
        store.close()

    # A new coordinator has no transient lifecycle state. The worker is also
    # deliberately made to forget the run: the persisted result must still
    # drive terminalization without another status request.
    backend.status_payload = {"known": False, "terminal": False, "result": None}
    backend.status_targets.clear()
    reopened = SQLiteStateStore(path)
    restarted = SchedulerCoordinator(
        reopened,
        TypedWorkspaceManager(),
        DriverRegistry((OperationsHarnessDescriptor(),)),
        backends=backends,
    )
    try:
        finalized = asyncio.run(restarted.reconcile_managed_runs())
        assert [item.id for item in finalized] == [job.id]
        assert reopened.get_job(job.id).state is JobState.COMPLETED
        assert reopened.get_run(run.id).state is RunState.COMPLETED
        assert reopened.list_artifacts(job_id=job.id)[0].ref == IMAGE
        assert backend.status_calls == status_calls_before_restart
        assert backend.status_targets == []
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("status_payload", "expected_job_state", "expected_run_state"),
    (
        (
            {"known": True, "terminal": False, "result": None},
            JobState.RUNNING,
            RunState.RUNNING,
        ),
        (
            {"known": False, "terminal": False, "result": None},
            JobState.FAILED,
            RunState.FAILED,
        ),
    ),
)
def test_recovery_reconciles_typed_starting_intent_without_restarting_operation(
    tmp_path,
    make_application_job,
    status_payload,
    expected_job_state,
    expected_run_state,
):
    path = tmp_path / "typed-starting-crash.sqlite"
    store, plane, backend, backends, job = _setup(make_application_job, path=path)
    try:
        run = _seed_starting_operation_intent(store, plane, backend, job)
    finally:
        store.close()

    backend.status_payload = status_payload
    reopened = SQLiteStateStore(path)
    restarted = SchedulerCoordinator(
        reopened,
        TypedWorkspaceManager(),
        DriverRegistry((OperationsHarnessDescriptor(),)),
        backends=backends,
    )
    try:
        asyncio.run(restarted.recover_managed_runs())

        assert reopened.get_job(job.id).state is expected_job_state
        assert reopened.get_run(run.id).state is expected_run_state
        assert backend.status_targets == [run.id]
        assert backend.dispatches == 0
        if expected_job_state is JobState.RUNNING:
            assert reopened.find_active_allocation(job.id) is not None
            assert reopened.find_active_reservation(job.id) is not None
        else:
            assert reopened.find_active_allocation(job.id) is None
            assert reopened.find_active_reservation(job.id) is None
            assert reopened.find_workspace(job.id).state is WorkspaceState.RELEASED
    finally:
        reopened.close()


def test_remote_build_without_verification_capability_is_not_dispatched(
    make_application_job,
):
    store, plane, _backend, _backends, job = _setup(
        make_application_job,
        verifies_artifacts=False,
    )
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is None
        assert plane.inspect_job(job.id).state is JobState.READY
        assert store.list_artifacts(job_id=job.id) == []
        assert store.list_runs(job.id) == []
        assert store.find_active_allocation(job.id) is None
        assert store.find_active_reservation(job.id) is None
    finally:
        store.close()


def test_build_output_is_not_verified_if_capability_disappears(
    make_application_job,
):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        # Model a worker that was initially admitted after heartbeat discovery
        # but no longer proves artifact verification at terminalization.
        backend.verifies_artifacts = False
        backend.status_payload = {
            "known": True,
            "terminal": True,
            "result": RunResult(
                RunOutcome.COMPLETED,
                "caller asserted a digest",
                produced_artifacts=(ProducedArtifact("image", IMAGE),),
            ).to_dict(),
        }

        with pytest.raises(LifecycleError, match="trusted artifact verification"):
            asyncio.run(plane.reconcile_managed_runs())

        assert plane.inspect_job(job.id).state is JobState.RUNNING
        assert store.get_run(run.id).state is RunState.RUNNING
        assert store.list_artifacts(job_id=job.id) == []
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
    finally:
        store.close()


def test_reconcile_failed_and_unknown_typed_runs_fail_closed_and_clean(
    make_application_job,
):
    for payload, expected_summary in (
        (
            {
                "known": True,
                "terminal": True,
                "result": RunResult(
                    RunOutcome.FAILED,
                    "build failed",
                    consumed_quota=1,
                ).to_dict(),
            },
            "build failed",
        ),
        (
            {"known": False, "terminal": False, "result": None},
            "worker lost the typed run state after restart",
        ),
    ):
        store, plane, backend, _backends, job = _setup(make_application_job)
        try:
            run = asyncio.run(plane.dispatch_next())
            assert run is not None
            backend.status_payload = payload
            finalized = asyncio.run(plane.reconcile_managed_runs())
            assert [item.id for item in finalized] == [job.id]
            assert plane.inspect_job(job.id).state is JobState.FAILED
            stored = store.get_run(run.id)
            assert stored.state is RunState.FAILED
            assert stored.result is not None
            assert stored.result.summary == expected_summary
            assert store.list_artifacts(job_id=job.id) == []
            assert store.find_active_allocation(job.id) is None
            assert store.find_active_reservation(job.id) is None
        finally:
            store.close()


def test_reconcile_active_or_missing_backend_is_retryable(make_application_job):
    store, plane, backend, backends, job = _setup(make_application_job)
    try:
        run = asyncio.run(plane.dispatch_next())
        assert run is not None
        assert asyncio.run(plane.reconcile_managed_runs()) == ()
        assert plane.inspect_job(job.id).state is JobState.RUNNING
        assert backend.status_calls == 1

        backends.remove("remote-a")
        with pytest.raises(LifecycleError, match="unavailable backend"):
            asyncio.run(plane.reconcile_managed_runs())
        assert plane.inspect_job(job.id).state is JobState.RUNNING
        assert store.find_active_allocation(job.id) is not None
        assert store.find_active_reservation(job.id) is not None
    finally:
        store.close()


def test_daemon_tick_automatically_finalizes_typed_operation(make_application_job):
    store, plane, backend, _backends, job = _setup(make_application_job)
    try:
        daemon = AgentDaemon(
            plane,
            worker_heartbeat_seconds=60,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        first = asyncio.run(daemon.tick())
        assert first is not None
        backend.status_payload = {
            "known": True,
            "terminal": True,
            "result": RunResult(
                RunOutcome.COMPLETED,
                "image built",
                consumed_quota=1,
                produced_artifacts=(ProducedArtifact("image", IMAGE),),
            ).to_dict(),
        }
        assert asyncio.run(daemon.tick()) is None
        assert plane.inspect_job(job.id).state is JobState.COMPLETED
    finally:
        store.close()
