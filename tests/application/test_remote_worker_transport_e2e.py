from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import ArtifactKind, JobState, RunOutcome, WorkspaceState
from agentd.domain.models import (
    ArtifactRecord,
    ArtifactRef,
    ArtifactSpec,
    BuildImageOperation,
    HarnessCapabilities,
    ProducedArtifact,
    QuotaPool,
    ResourceVector,
    RunResult,
    WorkerNode,
)
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers import (
    BackendRegistry,
    OperationJournal,
    RemoteWorkerBackend,
    RemoteWorkerClient,
    WorkerServer,
)
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE

SECRET = b"e" * 32
GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 40)
IMAGE = ArtifactRef(
    ArtifactKind.OCI_IMAGE,
    "ghcr.io/acme/app@sha256:" + "c" * 64,
)


class ExplodingWorkspaceManager:
    """Typed remote operations must not use a controller-local workspace."""

    def allocate(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote typed operation allocated a local workspace")

    def release(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote typed operation released a local workspace")

    def is_available(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote typed operation inspected a local workspace")

    def current_commit(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote typed operation inspected a local commit")

    def commit_changes(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote typed operation committed locally")


class TerminalTypedDriver(FakeHarnessDriver):
    """Controlled worker-side typed driver; no subprocess or Docker is used."""

    def __init__(self, result: RunResult) -> None:
        super().__init__(
            capabilities=HarnessCapabilities(
                name="typed-transport-test",
                models=frozenset({"standard"}),
                features=frozenset({ARTIFACT_VERIFICATION_FEATURE, "build-image"}),
            ),
            result=result,
            id_factory=lambda: "worker-handle-1",
        )
        self._terminal_result = result
        self.status_calls = 0

    def status(self, _run: object) -> dict[str, object]:
        self.status_calls += 1
        return {
            "known": True,
            "terminal": True,
            "result": self._terminal_result.to_dict(),
        }


def _external_artifact() -> ArtifactRecord:
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


def test_authenticated_remote_scheduler_reconcile_publishes_and_cleans_up(
    tmp_path: Path,
    make_application_job,
) -> None:
    async def scenario() -> None:
        result = RunResult(
            outcome=RunOutcome.COMPLETED,
            summary="controlled remote build completed",
            consumed_quota=3,
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        )
        worker_driver = TerminalTypedDriver(result)
        controller_driver = TerminalTypedDriver(result)
        store = SQLiteStateStore(tmp_path / "control.sqlite3")
        journal = OperationJournal(
            tmp_path / "worker.sqlite3",
            node_id="remote-node",
            session_epoch="epoch-1",
        )
        server = WorkerServer(
            "127.0.0.1",
            0,
            node_id="remote-node",
            session_epoch="epoch-1",
            secret=SECRET,
            drivers=[worker_driver],
            journal=journal,
            allow_insecure_loopback=True,
            request_timeout_seconds=1,
            shutdown_timeout_seconds=1,
        )
        await server.start()
        host, port = server.address
        client = RemoteWorkerClient(
            host,
            port,
            node_id="remote-node",
            session_epoch="epoch-1",
            secret=SECRET,
            allow_insecure_loopback=True,
            request_timeout_seconds=1,
        )
        backend = RemoteWorkerBackend(
            client,
            name="remote",
            node_id="remote-node",
            expected_driver="typed-transport-test",
            features=frozenset({ARTIFACT_VERIFICATION_FEATURE, "build-image"}),
        )
        coordinator = SchedulerCoordinator(
            store,
            ExplodingWorkspaceManager(),
            DriverRegistry((controller_driver,)),
            backends=BackendRegistry((backend,)),
        )
        plane = ControlPlane(store, coordinator=coordinator)
        plane.register_node(
            WorkerNode(
                id="remote-node",
                labels={
                    "os": "linux",
                    "arch": "x86_64",
                    "backend": "remote",
                },
                capacity=ResourceVector(cpu=8, ram_gb=32),
                harnesses=frozenset({"typed-transport-test"}),
            )
        )
        plane.register_quota_pool(
            QuotaPool(id="default", provider="remote", remaining=100)
        )
        plane.register_external_artifact(_external_artifact())
        job = make_application_job(
            id="remote-build",
            allowed_harnesses=("typed-transport-test",),
            preferred_harnesses=("typed-transport-test",),
            artifact_inputs=(GIT,),
            artifact_outputs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
            operation=BuildImageOperation(
                source_input=GIT,
                source_repository="https://github.com/acme/app.git",
                output_name="image",
                registry_repository="ghcr.io/acme/app",
            ),
        )
        plane.submit(job)
        try:
            snapshots = await plane.refresh_worker_heartbeats()
            assert snapshots[0]["node_id"] == "remote-node"

            run = await plane.dispatch_next()
            assert run is not None
            assert run.backend == "remote"
            assert run.contract.operation == job.operation
            assert run.contract.allowed_filesystem_scope == ()

            # This status crosses the real TCP framing + HMAC path before the
            # scheduler's reconciliation call performs the terminal handoff.
            status = await backend.status(run.id)
            assert status["known"] is True
            assert status["terminal"] is True

            finalized = await plane.reconcile_managed_runs()
            assert tuple(item.id for item in finalized) == (job.id,)
            assert store.get_job(job.id).state is JobState.COMPLETED
            artifacts = store.list_artifacts(job_id=job.id)
            assert len(artifacts) == 1
            assert artifacts[0].ref == IMAGE
            assert artifacts[0].producer_run_id == run.id
            assert store.get_run(run.id).result == result

            heartbeat = await client.heartbeat(request_id="post-reconcile-heartbeat")
            assert heartbeat["active_runs"] == 0
            workspace = store.find_workspace(job.id)
            assert workspace is not None
            assert workspace.state is WorkspaceState.RELEASED
            assert store.find_active_allocation(job.id) is None
            assert store.find_active_reservation(job.id) is None

            # Terminal completion is retry-safe and does not duplicate the
            # immutable artifact publication.
            assert await plane.complete(job.id) == store.get_job(job.id)
            assert len(store.list_artifacts(job_id=job.id)) == 1
            assert worker_driver.status_calls == 2
        finally:
            await backend.close()
            await server.close()
            store.close()

    asyncio.run(scenario())
