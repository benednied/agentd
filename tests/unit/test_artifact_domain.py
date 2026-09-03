from __future__ import annotations

from dataclasses import replace

import pytest

from agentd.domain.enums import ArtifactKind, OperationKind, RunOutcome
from agentd.domain.models import (
    ArtifactRef,
    ArtifactSelector,
    ArtifactSpec,
    BuildImageOperation,
    DeployImageOperation,
    ExecutionContract,
    Job,
    ProducedArtifact,
    RunResult,
)

GIT_SHA = "a" * 40
OCI_DIGEST = "ghcr.io/acme/app@sha256:" + "b" * 64


def test_artifact_refs_are_canonical_and_round_trip() -> None:
    git = ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA)
    image = ArtifactRef(ArtifactKind.OCI_IMAGE, OCI_DIGEST)

    assert ArtifactRef.from_dict(git.to_dict()) == git
    assert ArtifactRef.from_dict(image.to_dict()) == image
    assert image.value == image.value.lower()

    with pytest.raises(ValueError):
        ArtifactRef(ArtifactKind.GIT_COMMIT, "A" * 40)
    with pytest.raises(ValueError):
        ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 39)
    for value in (
        "ghcr.io/acme/app:latest",
        "ghcr.io/acme/app",
        "GHCR.IO/acme/app@sha256:" + "b" * 64,
    ):
        with pytest.raises(ValueError):
            ArtifactRef(ArtifactKind.OCI_IMAGE, value)


def test_selector_and_typed_operations_round_trip() -> None:
    selector = ArtifactSelector("build-job", "image", ArtifactKind.OCI_IMAGE)
    source = ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA)
    build = BuildImageOperation(
        source_input=source,
        source_repository="https://github.com/acme/app.git",
        output_name="image",
        registry_repository="ghcr.io/acme/app",
        context="container",
        dockerfile="container/Dockerfile",
        platforms=("linux/amd64", "linux/arm64"),
    )
    deploy = DeployImageOperation(
        image_input=selector,
        target="staging",
        config_revision=source,
        deployment_name="web",
    )

    assert BuildImageOperation.from_dict(build.to_dict()) == build
    assert DeployImageOperation.from_dict(deploy.to_dict()) == deploy
    assert deploy.kind is OperationKind.DEPLOY_IMAGE


@pytest.mark.parametrize(
    ("context", "dockerfile"),
    (("../outside", "Dockerfile"), (".", "--unsafe"), ("$(id)", "Dockerfile")),
)
def test_build_operation_rejects_unconfined_or_shell_paths(
    context: str, dockerfile: str
) -> None:
    with pytest.raises(ValueError):
        BuildImageOperation(
            source_input=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
            source_repository="/workspace/repository",
            output_name="image",
            registry_repository="ghcr.io/acme/app",
            context=context,
            dockerfile=dockerfile,
        )


@pytest.mark.parametrize(
    "repository",
    (
        "ghcr.io/acme/app:latest",
        "ghcr.io/acme/app@sha256:" + "b" * 64,
        "ghcr.io/acme/App",
    ),
)
def test_build_operation_rejects_tagged_or_noncanonical_repository(
    repository: str,
) -> None:
    with pytest.raises(ValueError):
        BuildImageOperation(
            source_input=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
            source_repository="https://github.com/acme/app.git",
            output_name="image",
            registry_repository=repository,
        )


def test_build_operation_rejects_credential_url_and_shell_fields() -> None:
    with pytest.raises(ValueError):
        BuildImageOperation(
            source_input=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
            source_repository="https://user:password@github.com/acme/app.git",
            output_name="image",
            registry_repository="ghcr.io/acme/app",
        )
    with pytest.raises(ValueError):
        DeployImageOperation(
            image_input=ArtifactRef(ArtifactKind.OCI_IMAGE, OCI_DIGEST),
            target="prod;rm",
            config_revision=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
            deployment_name="web",
        )


@pytest.mark.parametrize(
    "repository",
    (
        "git@github.com:acme/app.git",
        "ssh://git@github.com/acme/app.git",
        "http://github.com/acme/app.git",
    ),
)
def test_build_operation_rejects_ssh_scp_and_insecure_remote_sources(
    repository: str,
) -> None:
    with pytest.raises(ValueError, match=r"HTTPS|SSH|scp"):
        BuildImageOperation(
            source_input=ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),
            source_repository=repository,
            output_name="image",
            registry_repository="ghcr.io/acme/app",
        )


def test_job_contract_and_run_result_keep_legacy_defaults(make_job) -> None:
    selector = ArtifactSelector("build-job", "image", ArtifactKind.OCI_IMAGE)
    spec = ArtifactSpec("image", ArtifactKind.OCI_IMAGE, "application/vnd.oci.image")
    job = make_job(artifact_inputs=(selector,), artifact_outputs=(spec,))
    assert Job.from_dict(job.to_dict()) == job

    legacy = job.to_dict()
    legacy.pop("artifact_inputs")
    legacy.pop("artifact_outputs")
    legacy.pop("operation")
    assert Job.from_dict(legacy).artifact_inputs == ()

    contract = ExecutionContract(
        job_id=job.id,
        objective=job.objective,
        scope="repo",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=("/workspace",),
        checkpoint_expectations="safe boundaries",
        coordination_mechanisms=(),
        completion_protocol="report",
        working_directory="/workspace",
        environment={},
        model_class="standard",
        artifact_inputs=(ArtifactRef(ArtifactKind.GIT_COMMIT, GIT_SHA),),
        artifact_outputs=(spec,),
    )
    assert ExecutionContract.from_dict(contract.to_dict()) == contract
    legacy_contract = contract.to_dict()
    legacy_contract.pop("artifact_inputs")
    legacy_contract.pop("artifact_outputs")
    legacy_contract.pop("operation")
    assert ExecutionContract.from_dict(legacy_contract).artifact_inputs == ()

    output = ProducedArtifact("image", ArtifactRef(ArtifactKind.OCI_IMAGE, OCI_DIGEST))
    result = RunResult(RunOutcome.COMPLETED, produced_artifacts=(output,))
    assert RunResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="unique spec"):
        RunResult(
            RunOutcome.COMPLETED,
            produced_artifacts=(output, replace(output, metadata={"duplicate": True})),
        )
