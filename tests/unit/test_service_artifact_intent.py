from __future__ import annotations

import pytest

from agentd.domain.enums import ArtifactKind
from agentd.domain.models import (
    ArtifactRef,
    ArtifactSelector,
    ArtifactSpec,
    BuildImageOperation,
    DeployImageOperation,
)
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

GIT_SHA = "a" * 40
OTHER_GIT_SHA = "c" * 40


def _build_job(make_job, *, source_input, output_specs, operation):
    return make_job(
        id="build-job",
        artifact_inputs=(source_input,),
        artifact_outputs=tuple(output_specs),
        operation=operation,
    )


def test_submit_accepts_a_valid_typed_build_intent(make_job) -> None:
    source = ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA)
    operation = BuildImageOperation(
        source_input=source,
        source_repository="https://github.com/acme/app.git",
        output_name="image",
        registry_repository="ghcr.io/acme/app",
    )
    store = SQLiteStateStore()
    submitted = ControlPlane(store).submit(
        _build_job(
            make_job,
            source_input=source,
            output_specs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
            operation=operation,
        )
    )

    assert submitted.artifact_outputs[0].name == "image"


def test_submit_rejects_build_input_and_output_mismatches(make_job) -> None:
    source = ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA)
    operation = BuildImageOperation(
        source_input=source,
        source_repository="/workspace/repository",
        output_name="image",
        registry_repository="ghcr.io/acme/app",
    )
    store = SQLiteStateStore()
    with pytest.raises(ValueError, match="source_input"):
        ControlPlane(store).submit(
            _build_job(
                make_job,
                source_input=ArtifactRef(ArtifactKind.GIT_COMMIT, OTHER_GIT_SHA),
                output_specs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
                operation=operation,
            )
        )

    with pytest.raises(ValueError, match="OCI_IMAGE"):
        ControlPlane(SQLiteStateStore()).submit(
            _build_job(
                make_job,
                source_input=source,
                output_specs=(ArtifactSpec("image", ArtifactKind.GIT_COMMIT),),
                operation=operation,
            )
        )

    with pytest.raises(ValueError, match="exactly one"):
        ControlPlane(SQLiteStateStore()).submit(
            _build_job(
                make_job,
                source_input=source,
                output_specs=(
                    ArtifactSpec("image", ArtifactKind.OCI_IMAGE),
                    ArtifactSpec("other", ArtifactKind.OCI_IMAGE),
                ),
                operation=operation,
            )
        )


def test_submit_rejects_selectors_outside_dependency_dag(make_job) -> None:
    selector = ArtifactSelector("producer-job", "image", ArtifactKind.OCI_IMAGE)
    with pytest.raises(ValueError, match="dependencies"):
        ControlPlane(SQLiteStateStore()).submit(
            make_job(
                id="consumer-job",
                artifact_inputs=(selector,),
            )
        )


def test_submit_accepts_build_to_deploy_predeclaration_without_store_lookup(
    make_job,
) -> None:
    build_source = ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA)
    build = _build_job(
        make_job,
        source_input=build_source,
        output_specs=(ArtifactSpec("image", ArtifactKind.OCI_IMAGE),),
        operation=BuildImageOperation(
            source_input=build_source,
            source_repository="/workspace/repository",
            output_name="image",
            registry_repository="ghcr.io/acme/app",
        ),
    )
    selector = ArtifactSelector("build-job", "image", ArtifactKind.OCI_IMAGE)
    deploy = make_job(
        id="deploy-job",
        dependencies=("build-job",),
        artifact_inputs=(selector,),
        artifact_outputs=(),
        operation=DeployImageOperation(
            image_input=selector,
            target="staging",
            config_revision=ArtifactRef(ArtifactKind.GIT_COMMIT, OTHER_GIT_SHA),
            deployment_name="web",
        ),
    )
    store = SQLiteStateStore()
    plane = ControlPlane(store)

    assert plane.submit(build).id == "build-job"
    # The deploy submit does not require a pre-registered config artifact.
    # Dispatch-time resolution/registration owns that check.
    assert plane.submit(deploy).id == "deploy-job"


def test_submit_rejects_deploy_outputs_and_selector_mismatch(make_job) -> None:
    selector = ArtifactSelector("build-job", "image", ArtifactKind.OCI_IMAGE)
    with pytest.raises(ValueError, match="image_input"):
        ControlPlane(SQLiteStateStore()).submit(
            make_job(
                id="deploy-job",
                dependencies=("build-job",),
                artifact_inputs=(
                    ArtifactSelector("build-job", "other", ArtifactKind.OCI_IMAGE),
                ),
                operation=DeployImageOperation(
                    image_input=selector,
                    target="staging",
                    config_revision=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
                    deployment_name="web",
                ),
            )
        )

    with pytest.raises(ValueError, match="outputs"):
        ControlPlane(SQLiteStateStore()).submit(
            make_job(
                id="deploy-job-outputs",
                dependencies=("build-job",),
                artifact_inputs=(selector,),
                artifact_outputs=(ArtifactSpec("unexpected", ArtifactKind.OCI_IMAGE),),
                operation=DeployImageOperation(
                    image_input=selector,
                    target="staging",
                    config_revision=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
                    deployment_name="web",
                ),
            )
        )
