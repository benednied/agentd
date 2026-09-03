from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agentd.coordinator import LifecycleError, SchedulerCoordinator
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
    ArtifactSelector,
    ArtifactSpec,
    BuildImageOperation,
    DeployImageOperation,
    HarnessCapabilities,
    ProducedArtifact,
    QuotaPool,
    ResourceVector,
    RunResult,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import transition_job
from agentd.harness import DriverRegistry, FakeHarnessDriver
from agentd.service import ControlPlane
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE

GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 40)
TRUSTED_GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "f" * 40)
CONFIG = ArtifactRef(ArtifactKind.GIT_COMMIT, "c" * 40)
IMAGE = ArtifactRef(
    ArtifactKind.OCI_IMAGE,
    "ghcr.io/acme/app@sha256:" + "b" * 64,
)
CONFLICT_IMAGE = ArtifactRef(
    ArtifactKind.OCI_IMAGE,
    "ghcr.io/acme/app@sha256:" + "c" * 64,
)
IMAGE_SPEC = ArtifactSpec("image", ArtifactKind.OCI_IMAGE)
VERIFIED_FAKE_CAPABILITIES = HarnessCapabilities(
    name="fake",
    models=frozenset({"standard"}),
    features=frozenset({ARTIFACT_VERIFICATION_FEATURE}),
)


class TinyWorkspaceManager:
    def __init__(self) -> None:
        self.leases: dict[str, WorkspaceLease] = {}

    def allocate(self, job, base_ref="HEAD") -> WorkspaceLease:
        lease = WorkspaceLease(
            id=f"workspace-{job.id}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/{job.id}",
            working_directory=f"/workspaces/{job.id}",
            base_ref=base_ref,
        )
        self.leases[lease.id] = lease
        return lease

    def release(self, lease: WorkspaceLease) -> WorkspaceLease:
        released = replace(lease, state=WorkspaceState.RELEASED)
        self.leases[lease.id] = released
        return released

    def is_available(self, lease: WorkspaceLease) -> bool:
        return self.leases.get(lease.id) == lease

    def current_commit(self, lease: WorkspaceLease) -> str:
        return lease.commit or "f" * 40

    def commit_changes(self, lease: WorkspaceLease) -> str:
        return self.current_commit(lease)


def _external(ref: ArtifactRef, name: str = "input") -> ArtifactRecord:
    from datetime import UTC, datetime

    return ArtifactRecord(
        id=f"external-{name}",
        ref=ref,
        producer_job_id=None,
        producer_run_id=None,
        spec_name=name,
        verified=True,
        verified_at=datetime.now(UTC),
        external=True,
    )


def _build_job(
    make_application_job,
    *,
    job_id: str = "build-job",
    output=True,
    output_name: str = "image",
):
    return make_application_job(
        id=job_id,
        artifact_inputs=(GIT,),
        artifact_outputs=(ArtifactSpec(output_name, ArtifactKind.OCI_IMAGE),)
        if output
        else (),
        operation=BuildImageOperation(
            source_input=GIT,
            source_repository="https://github.com/acme/app.git",
            output_name=output_name,
            registry_repository="ghcr.io/acme/app",
        ),
    )


def _register(rig, application_node, application_quota_pool) -> None:
    rig.plane.register_node(application_node)
    rig.plane.register_quota_pool(application_quota_pool)


def _verified_fake_driver() -> FakeHarnessDriver:
    return FakeHarnessDriver(capabilities=VERIFIED_FAKE_CAPABILITIES)


def test_unresolved_selector_skips_dispatch_without_side_effects(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    producer = make_application_job(id="producer")
    rig.plane.submit(producer)
    stored = rig.store.get_job(producer.id)
    admitted, admitted_event = transition_job(stored, JobState.ADMITTED, "seed")
    rig.store.save_job(admitted, admitted_event, expected=stored)
    running, running_event = transition_job(admitted, JobState.RUNNING, "seed")
    rig.store.save_job(running, running_event, expected=admitted)
    completed, completed_event = transition_job(running, JobState.COMPLETED, "seed")
    rig.store.save_job(completed, completed_event, expected=running)
    consumer = make_application_job(
        id="consumer",
        dependencies=(producer.id,),
        artifact_inputs=(
            ArtifactSelector(producer.id, "image", ArtifactKind.OCI_IMAGE),
        ),
    )
    rig.plane.submit(consumer)

    assert asyncio.run(rig.plane.dispatch_next()) is None
    assert rig.store.list_reservations(consumer.id) == []
    assert rig.store.list_allocations(consumer.id) == []
    assert rig.store.list_workspaces(consumer.id) == []
    assert rig.plane.inspect_job(consumer.id).state is JobState.READY


def test_unregistered_direct_ref_also_skips_dispatch_without_side_effects(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    job = make_application_job(id="unregistered", artifact_inputs=(GIT,))
    rig.plane.submit(job)

    assert asyncio.run(rig.plane.dispatch_next()) is None
    assert rig.store.list_reservations(job.id) == []
    assert rig.store.list_allocations(job.id) == []
    assert rig.store.list_workspaces(job.id) == []


def test_registered_verified_external_ref_is_resolved_in_build_contract(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(GIT, "source"))
    job = _build_job(make_application_job)
    rig.plane.submit(job)

    run = asyncio.run(rig.plane.dispatch_next())
    assert run is not None
    assert run.contract.artifact_inputs == (GIT,)
    assert isinstance(run.contract.operation, BuildImageOperation)
    assert run.contract.operation.source_input == GIT


def test_direct_ref_resolution_uses_value_with_multiple_provenance_rows(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(IMAGE, "image-a"))
    rig.store.register_external_artifact(_external(IMAGE, "image-b"))
    job = make_application_job(id="direct-image", artifact_inputs=(IMAGE,))
    rig.plane.submit(job)

    run = asyncio.run(rig.plane.dispatch_next())

    assert run is not None
    assert run.contract.artifact_inputs == (IMAGE,)


@pytest.mark.parametrize(
    ("produced_ref", "publishes"),
    ((GIT, False), (TRUSTED_GIT, True)),
)
def test_git_output_must_match_controller_observed_workspace_commit(
    make_application_rig,
    make_application_job,
    application_node,
    application_quota_pool,
    produced_ref,
    publishes,
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    job = make_application_job(
        id=f"git-output-{publishes}",
        artifact_outputs=(ArtifactSpec("source", ArtifactKind.GIT_COMMIT),),
    )
    rig.plane.submit(job)
    run = asyncio.run(rig.plane.dispatch_next())
    assert run is not None
    rig.driver.configure_result(
        run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "source produced",
            commit=GIT.value,
            produced_artifacts=(ProducedArtifact("source", produced_ref),),
        ),
    )

    if not publishes:
        with pytest.raises(LifecycleError, match="trusted workspace commit"):
            asyncio.run(rig.plane.complete(job.id))
        assert rig.plane.inspect_job(job.id).state is JobState.RUNNING
        assert rig.store.list_artifacts(job_id=job.id) == []
        return

    assert asyncio.run(rig.plane.complete(job.id)).state is JobState.COMPLETED
    artifact = rig.store.list_artifacts(job_id=job.id)[0]
    assert artifact.ref == TRUSTED_GIT
    assert artifact.verified is True
    stored_result = rig.store.get_run(run.id).result
    assert stored_result is not None
    assert stored_result.commit == TRUSTED_GIT.value
    assert stored_result.metadata["untrusted_reported_commit"] == GIT.value


@pytest.mark.parametrize(
    "produced",
    (
        (),
        (ProducedArtifact("image", GIT),),
        (ProducedArtifact("extra", IMAGE),),
    ),
)
def test_invalid_build_outputs_block_terminal_transition(
    make_application_rig,
    make_application_job,
    application_node,
    application_quota_pool,
    produced,
) -> None:
    rig = make_application_rig()
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(GIT, "source"))
    job = _build_job(make_application_job)
    rig.plane.submit(job)
    run = asyncio.run(rig.plane.dispatch_next())
    assert run is not None
    rig.driver.configure_result(
        run.handle,
        RunResult(RunOutcome.COMPLETED, "build complete", produced_artifacts=produced),
    )

    with pytest.raises(LifecycleError, match="output"):
        asyncio.run(rig.plane.complete(job.id))
    assert rig.plane.inspect_job(job.id).state is JobState.RUNNING
    assert rig.store.get_run(run.id).state is RunState.RUNNING
    assert rig.store.list_artifacts(job_id=job.id) == []


def test_valid_build_output_is_atomic_and_deploy_selector_resolves_after_restart(
    tmp_path: Path,
    make_application_job,
) -> None:
    path = tmp_path / "agentd.sqlite"
    store = SQLiteStateStore(path)
    workspace = TinyWorkspaceManager()
    driver = FakeHarnessDriver(
        capabilities=VERIFIED_FAKE_CAPABILITIES,
        result=RunResult(RunOutcome.COMPLETED, "done", consumed_quota=1),
    )
    coordinator = SchedulerCoordinator(store, workspace, DriverRegistry((driver,)))
    plane = ControlPlane(store, coordinator=coordinator)
    plane.register_node(
        WorkerNode(
            id="node-1",
            labels={"os": "linux", "arch": "x86_64"},
            capacity=ResourceVector(cpu=8, ram_gb=32),
            harnesses=frozenset({"fake"}),
        )
    )
    plane.register_quota_pool(QuotaPool(id="default", provider="test", remaining=100))
    store.register_external_artifact(_external(GIT, "source"))
    store.register_external_artifact(_external(CONFIG, "config"))
    build = _build_job(make_application_job)
    plane.submit(build)
    selector = ArtifactSelector(build.id, "image", ArtifactKind.OCI_IMAGE)
    deploy = make_application_job(
        id="deploy-job",
        dependencies=(build.id,),
        artifact_inputs=(selector,),
        operation=DeployImageOperation(
            image_input=selector,
            target="staging",
            config_revision=CONFIG,
            deployment_name="web",
        ),
    )
    plane.submit(deploy)
    build_run = asyncio.run(plane.dispatch_next())
    assert build_run is not None
    driver.configure_result(
        build_run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "built",
            consumed_quota=1,
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        ),
    )
    assert asyncio.run(plane.complete(build.id)).state is JobState.COMPLETED
    assert store.list_artifacts(job_id=build.id)[0].ref == IMAGE
    store.close()

    reopened = SQLiteStateStore(path)
    reopened_driver = FakeHarnessDriver(
        result=RunResult(RunOutcome.COMPLETED, "deployed", consumed_quota=1)
    )
    reopened_plane = ControlPlane(
        reopened,
        coordinator=SchedulerCoordinator(
            reopened,
            TinyWorkspaceManager(),
            DriverRegistry((reopened_driver,)),
        ),
    )
    deploy_run = asyncio.run(reopened_plane.dispatch_next())
    assert deploy_run is not None
    assert isinstance(deploy_run.contract.operation, DeployImageOperation)
    assert deploy_run.contract.operation.image_input == IMAGE
    assert deploy_run.contract.artifact_inputs == (IMAGE,)
    reopened.close()


def test_distinct_producers_may_atomically_complete_with_the_same_oci_digest(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig(driver=_verified_fake_driver())
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(GIT, "source"))
    first = _build_job(make_application_job, job_id="build-a")
    second = _build_job(make_application_job, job_id="build-b")
    rig.plane.submit(first)
    rig.plane.submit(second)

    first_run = asyncio.run(rig.plane.dispatch_next())
    assert first_run is not None
    rig.driver.configure_result(
        first_run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "first build",
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        ),
    )
    assert asyncio.run(rig.plane.complete(first.id)).state is JobState.COMPLETED

    second_run = asyncio.run(rig.plane.dispatch_next())
    assert second_run is not None
    rig.driver.configure_result(
        second_run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "second build",
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        ),
    )
    assert asyncio.run(rig.plane.complete(second.id)).state is JobState.COMPLETED

    records = rig.store.list_artifacts()
    assert len(records) == 3
    outputs = [item for item in records if not item.external]
    assert {item.producer_job_id for item in outputs} == {first.id, second.id}
    assert {item.ref for item in outputs} == {IMAGE}


def test_artifact_conflict_rolls_back_terminal_job_and_run_transition(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig(driver=_verified_fake_driver())
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(GIT, "source"))
    job = _build_job(make_application_job, job_id="conflicting-build")
    rig.plane.submit(job)
    run = asyncio.run(rig.plane.dispatch_next())
    assert run is not None
    conflicting = ArtifactRecord(
        id="preexisting-image-output",
        ref=CONFLICT_IMAGE,
        producer_job_id=job.id,
        producer_run_id=run.id,
        spec_name="image",
        verified=True,
        verified_at=run.started_at,
        created_at=run.started_at,
    )
    rig.store.publish_artifact(conflicting)
    rig.driver.configure_result(
        run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "built",
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        ),
    )

    with pytest.raises(ConcurrentStateError, match="producer slot"):
        asyncio.run(rig.plane.complete(job.id))
    assert rig.plane.inspect_job(job.id).state is JobState.RUNNING
    assert rig.store.get_run(run.id).state is RunState.RUNNING
    assert rig.store.list_artifacts(job_id=job.id) == [conflicting]


def test_selector_is_specific_to_producer_and_declared_slot(
    make_application_rig, make_application_job, application_node, application_quota_pool
) -> None:
    rig = make_application_rig(driver=_verified_fake_driver())
    _register(rig, application_node, application_quota_pool)
    rig.store.register_external_artifact(_external(GIT, "source"))
    other_producer = _build_job(
        make_application_job,
        job_id="producer-a-other",
        output_name="other",
    )
    matching_digest_producer = _build_job(
        make_application_job,
        job_id="producer-b-image",
    )
    rig.plane.submit(other_producer)
    rig.plane.submit(matching_digest_producer)

    first_run = asyncio.run(rig.plane.dispatch_next())
    assert first_run is not None
    rig.driver.configure_result(
        first_run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "other output",
            produced_artifacts=(ProducedArtifact("other", IMAGE),),
        ),
    )
    assert (
        asyncio.run(rig.plane.complete(other_producer.id)).state is JobState.COMPLETED
    )
    second_run = asyncio.run(rig.plane.dispatch_next())
    assert second_run is not None
    rig.driver.configure_result(
        second_run.handle,
        RunResult(
            RunOutcome.COMPLETED,
            "matching digest",
            produced_artifacts=(ProducedArtifact("image", IMAGE),),
        ),
    )
    assert (
        asyncio.run(rig.plane.complete(matching_digest_producer.id)).state
        is JobState.COMPLETED
    )

    consumer = make_application_job(
        id="selector-consumer",
        dependencies=(other_producer.id, matching_digest_producer.id),
        artifact_inputs=(
            ArtifactSelector(other_producer.id, "image", ArtifactKind.OCI_IMAGE),
        ),
    )
    rig.plane.submit(consumer)

    assert asyncio.run(rig.plane.dispatch_next()) is None
    assert rig.plane.inspect_job(consumer.id).state is JobState.READY
