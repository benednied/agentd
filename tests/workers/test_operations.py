from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
from copy import deepcopy
from pathlib import Path

import pytest

import agentd.workers.operations as operations
from agentd.domain.enums import ArtifactKind, RunOutcome
from agentd.domain.models import (
    ArtifactRef,
    BuildImageOperation,
    DeployImageOperation,
    ExecutionContract,
    ProducedArtifact,
    ResumeCapsule,
)
from agentd.workers.operations import (
    CommandResult,
    DeploymentTarget,
    DockerComposeDeployer,
    DockerImageBuilder,
    GitRepositoryCache,
    GitWorkspace,
    OperationError,
    OperationHarnessDriver,
    SubprocessCommandRunner,
)

GIT = ArtifactRef(ArtifactKind.GIT_COMMIT, "a" * 40)
GIT_CONFIG = ArtifactRef(ArtifactKind.GIT_COMMIT, "c" * 40)
IMAGE = "ghcr.io/acme/app"
DIGEST = "sha256:" + "b" * 64
IMAGE_REF = ArtifactRef(ArtifactKind.OCI_IMAGE, f"{IMAGE}@{DIGEST}")
REPOSITORY = "https://github.com/acme/app.git"


class FakeCommandRunner:
    def __init__(
        self,
        *,
        metadata: dict | None = None,
        compose_config: dict | None = None,
        git_commit_override: str | None = None,
    ) -> None:
        self.calls: list[
            tuple[tuple[str, ...], Path | None, dict[str, str] | None]
        ] = []
        self.metadata = metadata or {"containerimage.digest": DIGEST}
        self.compose_config = compose_config
        self.git_commit_override = git_commit_override
        self.fail_next = False

    async def run(self, argv, *, cwd=None, environment=None, timeout=None):
        del timeout
        args = tuple(argv)
        self.calls.append((args, cwd, dict(environment) if environment else None))
        assert not {"ssh", "scp", "sh", "-c"}.intersection(args)
        if self.fail_next and "up" in args:
            self.fail_next = False
            raise OperationError("command failed")
        if args[:2] == ("git", "clone"):
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        if "worktree" in args and "add" in args:
            worktree = Path(args[-2])
            worktree.mkdir(parents=True, exist_ok=True)
            (worktree / "compose.yml").write_text("services: {}\n")
        if args[-2:] == ("--format", "json"):
            config = (
                deepcopy(self.compose_config)
                if self.compose_config is not None
                else {
                    "services": {
                        "app": {
                            "image": environment["AGENTD_IMAGE"],
                            "environment": {
                                "AGENTD_CONFIG_REVISION": environment[
                                    "AGENTD_CONFIG_REVISION"
                                ]
                            },
                        }
                    }
                }
            )
            return CommandResult(0, json.dumps(config).encode())
        if "rev-parse" in args:
            revision = args[-1]
            if revision == "HEAD^{commit}":
                revision = Path(args[args.index("-C") + 1]).name.rsplit("-", 1)[-1]
            elif revision.endswith("^{commit}"):
                revision = revision.removesuffix("^{commit}")
                revision = self.git_commit_override or revision
            return CommandResult(0, revision.encode() + b"\n")
        if "imagetools" in args:
            return CommandResult(0, (f"Name: {args[-1]}\nDigest: {DIGEST}\n").encode())
        if "buildx" in args and "build" in args:
            metadata_file = Path(args[args.index("--metadata-file") + 1])
            metadata_file.parent.mkdir(parents=True, exist_ok=True)
            metadata_file.write_text(json.dumps(self.metadata))
        return CommandResult(0)


def contract(operation) -> ExecutionContract:
    return ExecutionContract(
        job_id="job",
        objective="typed operation",
        scope="worker",
        acceptance_criteria=(),
        dependency_results={},
        role="worker",
        allowed_filesystem_scope=(),
        checkpoint_expectations="none",
        coordination_mechanisms=(),
        completion_protocol="return result",
        working_directory="",
        environment={},
        model_class="standard",
        resume=ResumeCapsule(),
        operation=operation,
    )


def build_operation() -> BuildImageOperation:
    return BuildImageOperation(
        source_input=GIT,
        source_repository=REPOSITORY,
        output_name="image",
        registry_repository=IMAGE,
    )


def deploy_operation() -> DeployImageOperation:
    return DeployImageOperation(
        image_input=IMAGE_REF,
        target="staging",
        config_revision=GIT,
        deployment_name="web",
    )


def deployment_target(
    environment: dict[str, str] | None = None,
) -> DeploymentTarget:
    return DeploymentTarget(
        Path("compose.yml"),
        REPOSITORY,
        environment or {},
    )


def compose_deployer(
    tmp_path: Path,
    runner: FakeCommandRunner,
    *,
    targets: dict[str, DeploymentTarget] | None = None,
) -> DockerComposeDeployer:
    return DockerComposeDeployer(
        targets if targets is not None else {"staging": deployment_target()},
        tmp_path / "state",
        frozenset({REPOSITORY}),
        frozenset({IMAGE}),
        runner,
    )


def test_subprocess_runner_drains_incremental_output() -> None:
    runner = SubprocessCommandRunner(timeout_seconds=2, max_output_bytes=128)
    script = (
        "import sys,time; "
        "sys.stdout.write('first'); sys.stdout.flush(); "
        "time.sleep(0.02); sys.stdout.write('-second')"
    )

    result = asyncio.run(runner.run((sys.executable, "-c", script)))

    assert result.stdout == b"first-second"


def test_subprocess_runner_does_not_inherit_process_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/trusted/bin")
    monkeypatch.setenv("HOME", "/trusted/home")
    monkeypatch.setenv("DOCKER_CONFIG", "/trusted/docker")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    script = "import json, os; print(json.dumps(dict(os.environ)))"

    result = asyncio.run(
        SubprocessCommandRunner(timeout_seconds=2).run((sys.executable, "-c", script))
    )
    child_environment = json.loads(result.stdout)

    assert child_environment["PATH"] == "/trusted/bin"
    assert child_environment["HOME"] == "/trusted/home"
    assert child_environment["DOCKER_CONFIG"] == "/trusted/docker"
    assert child_environment["HTTPS_PROXY"] == "http://proxy.example"
    assert "AWS_SECRET_ACCESS_KEY" not in child_environment
    assert "OPENAI_API_KEY" not in child_environment
    assert "SSH_AUTH_SOCK" not in child_environment


def test_compose_environment_uses_only_allowlisted_process_values_and_target_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("DOCKER_HOST", "unix:///operator/docker.sock")
    runner = FakeCommandRunner()
    deployer = compose_deployer(
        tmp_path,
        runner,
        targets={
            "staging": deployment_target(
                {
                    "TARGET_SECRET": "explicitly-trusted",
                    "AGENTD_IMAGE_DIGEST": "target-cannot-override-operation",
                }
            )
        },
    )

    asyncio.run(deployer.deploy(deploy_operation()))

    environment = runner.calls[-1][2]
    assert environment is not None
    assert environment["DOCKER_HOST"] == "unix:///operator/docker.sock"
    assert environment["TARGET_SECRET"] == "explicitly-trusted"
    assert environment["AGENTD_IMAGE_DIGEST"] == IMAGE_REF.value
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_git_cache_fetches_verifies_full_sha_and_cleans(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    cache = GitRepositoryCache(
        tmp_path, frozenset({"https://github.com/acme/app.git"}), runner
    )
    workspace = asyncio.run(cache.checkout("https://github.com/acme/app.git", GIT))
    assert workspace.path.parent == tmp_path / "worktrees"
    assert all(
        call[2]["GIT_CONFIG_NOSYSTEM"] == "1"
        and call[2]["GIT_CONFIG_GLOBAL"] == os.devnull
        for call in runner.calls
    )
    assert [call[0][:2] for call in runner.calls] == [
        ("git", "clone"),
        ("git", "-C"),
        ("git", "-C"),
        ("git", "-C"),
        ("git", "-C"),
        ("git", "-C"),
    ]
    asyncio.run(cache.cleanup(workspace))
    assert runner.calls[-1][0][0:4] == ("git", "-C", str(workspace.mirror), "worktree")


def test_git_cache_rejects_non_allowlisted_repo_and_missing_commit(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(git_commit_override=GIT.value)
    cache = GitRepositoryCache(
        tmp_path, frozenset({"https://github.com/acme/app.git"}), runner
    )
    with pytest.raises(OperationError, match="allowlisted"):
        asyncio.run(cache.checkout("https://github.com/evil/app.git", GIT))
    runner.calls.clear()
    bad = ArtifactRef(ArtifactKind.GIT_COMMIT, "c" * 40)
    with pytest.raises(OperationError, match="another object"):
        asyncio.run(cache.checkout("https://github.com/acme/app.git", bad))

    for forbidden in (
        "ssh://git.example/team/repo",
        "git@git.example:team/repo",
        "http://git.example/team/repo",
    ):
        restricted = GitRepositoryCache(
            tmp_path / "restricted",
            frozenset({forbidden}),
            runner,
        )
        with pytest.raises(OperationError, match=r"SSH/SCP|HTTPS or a local file"):
            asyncio.run(restricted.checkout(forbidden, GIT))


def test_git_cache_rejects_symlinked_mirror_and_worktree_paths(
    tmp_path: Path,
) -> None:
    repository = "https://github.com/acme/app.git"
    key = hashlib.sha256(repository.encode()).hexdigest()
    runner = FakeCommandRunner()

    symlink_root = tmp_path / "symlink-root-cache"
    symlink_root.mkdir()
    mirror_root_outside = tmp_path / "mirror-root-outside"
    mirror_root_outside.mkdir()
    (symlink_root / "mirrors").symlink_to(
        mirror_root_outside,
        target_is_directory=True,
    )
    with pytest.raises(OperationError, match="Mirror root must not be a symlink"):
        GitRepositoryCache(symlink_root, frozenset({repository}), runner)

    mirror_cache = GitRepositoryCache(
        tmp_path / "mirror-cache",
        frozenset({repository}),
        runner,
    )
    mirror_outside = tmp_path / "mirror-outside"
    mirror_outside.mkdir()
    (mirror_cache.root / "mirrors" / key).symlink_to(
        mirror_outside,
        target_is_directory=True,
    )
    with pytest.raises(OperationError, match="Mirror path must not be a symlink"):
        asyncio.run(mirror_cache.checkout(repository, GIT))

    worktree_cache = GitRepositoryCache(
        tmp_path / "worktree-cache",
        frozenset({repository}),
        runner,
    )
    mirror = worktree_cache.root / "mirrors" / key
    mirror.mkdir()
    worktree_outside = tmp_path / "worktree-outside"
    worktree_outside.mkdir()
    (worktree_cache.root / "worktrees" / f"{key}-{GIT.value}").symlink_to(
        worktree_outside,
        target_is_directory=True,
    )
    with pytest.raises(OperationError, match="Worktree path must not be a symlink"):
        asyncio.run(worktree_cache.checkout(repository, GIT))


def test_git_cache_rejects_cleanup_paths_outside_cache(tmp_path: Path) -> None:
    repository = "https://github.com/acme/app.git"
    cache = GitRepositoryCache(
        tmp_path / "cache",
        frozenset({repository}),
        FakeCommandRunner(),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = GitWorkspace(repository, GIT, outside, outside)

    with pytest.raises(OperationError, match="escapes the cache"):
        asyncio.run(cache.cleanup(workspace))


def test_git_cache_rejects_a_dirty_reused_worktree(tmp_path: Path) -> None:
    class DirtyWorktreeRunner(FakeCommandRunner):
        async def run(self, argv, *, cwd=None, environment=None, timeout=None):
            result = await super().run(
                argv,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
            )
            if "status" in tuple(argv):
                return CommandResult(0, b"?? injected.env\n")
            return result

    runner = DirtyWorktreeRunner()
    cache = GitRepositoryCache(
        tmp_path / "cache",
        frozenset({REPOSITORY}),
        runner,
    )

    with pytest.raises(OperationError, match="outside the commit"):
        asyncio.run(cache.checkout(REPOSITORY, GIT))


def test_image_build_is_digest_verified_and_cached(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)
    first = asyncio.run(builder.build(build_operation(), workspace))
    count = len(runner.calls)
    second = asyncio.run(builder.build(build_operation(), workspace))
    assert first == ProducedArtifact(
        "image",
        IMAGE_REF,
        metadata={
            "provenance": {
                "source_repository": "https://github.com/acme/app.git",
                "source_commit": GIT.value,
                "registry_repository": IMAGE,
                "output_name": "image",
                "context": ".",
                "dockerfile": "Dockerfile",
                "platforms": [],
            }
        },
    )
    assert second == first
    assert len(runner.calls) == count + 1
    assert runner.calls[0][0][0:4] == ("docker", "buildx", "build", "--push")
    assert (
        sum(
            call[0][:4] == ("docker", "buildx", "build", "--push")
            for call in runner.calls
        )
        == 1
    )


def test_image_build_rejects_unallowlisted_registry_and_invalid_digest(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(metadata={"containerimage.digest": "not-a-digest"})
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)
    with pytest.raises(OperationError, match="digest"):
        asyncio.run(builder.build(build_operation(), workspace))
    forbidden = BuildImageOperation(
        source_input=GIT,
        source_repository="https://github.com/acme/app.git",
        output_name="image",
        registry_repository="registry.example/other",
    )
    with pytest.raises(OperationError, match="allowlisted"):
        asyncio.run(builder.build(forbidden, workspace))


def test_image_build_uses_only_authoritative_output_digest_metadata(
    tmp_path: Path,
) -> None:
    unrelated = "sha256:" + "c" * 64
    runner = FakeCommandRunner(
        metadata={
            "buildx.build.provenance": {
                "materials": [{"digest": unrelated}],
            },
            "containerimage.digest": DIGEST,
            "containerimage.descriptor": {"digest": DIGEST},
        }
    )
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)

    artifact = asyncio.run(builder.build(build_operation(), workspace))

    assert artifact.ref == IMAGE_REF


@pytest.mark.parametrize(
    "metadata",
    (
        {"provenance": {"digest": DIGEST}},
        {
            "containerimage.digest": DIGEST,
            "containerimage.descriptor": {"digest": "sha256:" + "c" * 64},
        },
    ),
)
def test_image_build_rejects_ambiguous_or_unrelated_digest_metadata(
    tmp_path: Path,
    metadata: dict,
) -> None:
    runner = FakeCommandRunner(metadata=metadata)
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)

    with pytest.raises(OperationError, match="digest"):
        asyncio.run(builder.build(build_operation(), workspace))


def test_image_cache_is_provenance_bound_and_rejects_tampering(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)
    asyncio.run(builder.build(build_operation(), workspace))
    cache = next(
        (
            path
            for path in builder.cache_root.glob("*.json")
            if "metadata" not in path.name
        ),
        None,
    )
    assert cache is not None
    payload = json.loads(cache.read_text())
    payload["metadata"]["provenance"]["registry_repository"] = "evil.example/app"
    cache.write_text(json.dumps(payload))
    with pytest.raises(OperationError, match="provenance"):
        asyncio.run(builder.build(build_operation(), workspace))

    alternate = BuildImageOperation(
        source_input=GIT,
        source_repository="https://github.com/acme/app.git",
        output_name="image",
        registry_repository=IMAGE,
        context="container",
    )
    before = len(runner.calls)
    asyncio.run(builder.build(alternate, workspace))
    assert len(runner.calls) > before


def test_image_cache_reverifies_tampered_digest_without_rebuilding(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    workspace = GitWorkspace("https://github.com/acme/app.git", GIT, tmp_path, tmp_path)
    asyncio.run(builder.build(build_operation(), workspace))
    cache = next(
        path
        for path in builder.cache_root.glob("*.json")
        if "metadata" not in path.name
    )
    payload = json.loads(cache.read_text())
    payload["ref"]["value"] = f"{IMAGE}@sha256:{'c' * 64}"
    cache.write_text(json.dumps(payload))
    before = len(runner.calls)

    with pytest.raises(OperationError, match="digest"):
        asyncio.run(builder.build(build_operation(), workspace))

    assert len(runner.calls) == before + 1
    assert (
        sum(
            call[0][:4] == ("docker", "buildx", "build", "--push")
            for call in runner.calls
        )
        == 1
    )


def test_deployer_is_digest_pinned_idempotent_and_rolls_back(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(
        tmp_path,
        runner,
        targets={"staging": deployment_target({"FIXED": "yes"})},
    )
    asyncio.run(deployer.deploy(deploy_operation()))
    config_call = next(call for call in runner.calls if "config" in call[0])
    assert config_call[0][:5] == (
        "docker",
        "compose",
        "--project-name",
        "web",
        "-f",
    )
    assert Path(config_call[0][5]).name == "compose.yml"
    up_call = next(call for call in runner.calls if "up" in call[0])
    assert up_call[0][0:5] == (
        "docker",
        "compose",
        "--project-name",
        "web",
        "-f",
    )

    def up_calls():
        return [call for call in runner.calls if "up" in call[0]]

    assert len(up_calls()) == 1
    asyncio.run(deployer.deploy(deploy_operation()))
    assert len(up_calls()) == 1
    config_changed = DeployImageOperation(
        image_input=IMAGE_REF,
        target="staging",
        config_revision=GIT_CONFIG,
        deployment_name="web",
    )
    asyncio.run(deployer.deploy(config_changed))
    assert len(up_calls()) == 2
    assert up_calls()[-1][2]["AGENTD_CONFIG_REVISION"] == GIT_CONFIG.value
    assert GIT_CONFIG.value in up_calls()[-1][0][5]
    runner.fail_next = True
    changed = DeployImageOperation(
        image_input=ArtifactRef(ArtifactKind.OCI_IMAGE, f"{IMAGE}@sha256:{'c' * 64}"),
        target="staging",
        config_revision=GIT,
        deployment_name="web",
    )
    with pytest.raises(OperationError, match="rollback"):
        asyncio.run(deployer.deploy(changed))
    assert runner.calls[-1][2]["AGENTD_IMAGE_DIGEST"] == IMAGE_REF.value
    assert runner.calls[-1][2]["AGENTD_CONFIG_REVISION"] == GIT_CONFIG.value
    assert len(up_calls()) == 4


def test_deployer_rejects_mutable_image_in_resolved_compose_config(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(
        compose_config={
            "services": {
                "app": {
                    "image": "ghcr.io/acme/app:latest",
                    "environment": {"AGENTD_CONFIG_REVISION": GIT.value},
                }
            }
        }
    )
    deployer = compose_deployer(tmp_path, runner)

    with pytest.raises(OperationError, match="OCI digest"):
        asyncio.run(deployer.deploy(deploy_operation()))
    assert not [call for call in runner.calls if "up" in call[0]]


def test_deployer_rejects_digest_from_non_allowlisted_registry(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    operation = DeployImageOperation(
        image_input=ArtifactRef(
            ArtifactKind.OCI_IMAGE,
            "evil.example/team/app@sha256:" + "e" * 64,
        ),
        target="staging",
        config_revision=GIT,
        deployment_name="web",
    )

    with pytest.raises(OperationError, match="registry is not allowlisted"):
        asyncio.run(deployer.deploy(operation))
    assert runner.calls == []


def test_deployer_rejects_non_allowlisted_digest_pinned_sidecar(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(
        compose_config={
            "services": {
                "app": {
                    "image": IMAGE_REF.value,
                    "environment": {"AGENTD_CONFIG_REVISION": GIT.value},
                },
                "sidecar": {
                    "image": "evil.example/team/sidecar@sha256:" + "e" * 64,
                },
            }
        }
    )
    deployer = compose_deployer(tmp_path, runner)

    with pytest.raises(OperationError, match=r"sidecar.*not allowlisted"):
        asyncio.run(deployer.deploy(deploy_operation()))
    assert not [call for call in runner.calls if "up" in call[0]]


def test_deployer_requires_config_revision_on_image_service(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        compose_config={
            "services": {
                "app": {
                    "image": IMAGE_REF.value,
                    "environment": {},
                }
            }
        }
    )
    deployer = compose_deployer(tmp_path, runner)

    with pytest.raises(OperationError, match="config revision"):
        asyncio.run(deployer.deploy(deploy_operation()))
    assert not [call for call in runner.calls if "up" in call[0]]


def test_deployer_redeploys_when_resolved_compose_config_changes(
    tmp_path: Path,
) -> None:
    compose_config = {
        "services": {
            "app": {
                "image": IMAGE_REF.value,
                "environment": {"AGENTD_CONFIG_REVISION": GIT.value},
            }
        }
    }
    runner = FakeCommandRunner(compose_config=compose_config)
    deployer = compose_deployer(tmp_path, runner)

    asyncio.run(deployer.deploy(deploy_operation()))
    state_path = tmp_path / "state" / "staging-web.json"
    state = json.loads(state_path.read_text())
    assert state["config_digest"].startswith("sha256:")
    initial_up_count = sum("up" in call[0] for call in runner.calls)

    compose_config["services"]["app"]["labels"] = {"com.example.revision": "changed"}
    asyncio.run(deployer.deploy(deploy_operation()))

    assert sum("up" in call[0] for call in runner.calls) == initial_up_count + 1


def test_deployer_pending_write_failure_does_not_run_compose_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = operations.os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected pending fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(operations.os, "fsync", fail_first_fsync)
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)

    with pytest.raises(OperationError, match="intent"):
        asyncio.run(deployer.deploy(deploy_operation()))

    assert not [call for call in runner.calls if "up" in call[0]]
    assert not (tmp_path / "state" / "staging-web.pending.json").exists()


def test_deployer_applied_write_failure_rolls_back_and_restores_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    asyncio.run(deployer.deploy(deploy_operation()))
    state_path = tmp_path / "state" / "staging-web.json"
    previous = json.loads(state_path.read_text())
    config_changed = DeployImageOperation(
        image_input=IMAGE_REF,
        target="staging",
        config_revision=GIT_CONFIG,
        deployment_name="web",
    )

    real_fsync = operations.os.fsync
    calls = 0

    def fail_applied_write(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        # The next deploy's pending marker consumes two calls, then its
        # applied state file fsync is the third call.
        if calls == 3:
            raise OSError("injected applied-state fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(operations.os, "fsync", fail_applied_write)
    with pytest.raises(OperationError, match="persistence"):
        asyncio.run(deployer.deploy(config_changed))

    assert json.loads(state_path.read_text()) == previous
    assert not (tmp_path / "state" / "staging-web.pending.json").exists()
    up_calls = [call for call in runner.calls if "up" in call[0]]
    assert up_calls[-1][2]["AGENTD_CONFIG_REVISION"] == GIT.value


def test_deployer_applied_write_directory_fsync_failure_restores_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    asyncio.run(deployer.deploy(deploy_operation()))
    state_path = tmp_path / "state" / "staging-web.json"
    previous = json.loads(state_path.read_text())
    config_changed = DeployImageOperation(
        image_input=IMAGE_REF,
        target="staging",
        config_revision=GIT_CONFIG,
        deployment_name="web",
    )

    real_fsync = operations.os.fsync
    directory_fsyncs = 0

    def fail_applied_directory_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
            # The second deploy's pending marker is the first directory fsync;
            # its applied state reaches os.replace before the second one.
            if directory_fsyncs == 2:
                raise OSError("injected applied-state directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(operations.os, "fsync", fail_applied_directory_fsync)
    with pytest.raises(OperationError, match="persistence"):
        asyncio.run(deployer.deploy(config_changed))

    assert json.loads(state_path.read_text()) == previous
    assert not (tmp_path / "state" / "staging-web.pending.json").exists()


def test_deployer_pending_cleanup_failure_keeps_success_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)

    def fail_cleanup(cls, pending: Path) -> None:
        del cls, pending
        raise OSError("injected pending cleanup failure")

    monkeypatch.setattr(
        DockerComposeDeployer,
        "_remove_pending",
        classmethod(fail_cleanup),
    )
    asyncio.run(deployer.deploy(deploy_operation()))

    assert (tmp_path / "state" / "staging-web.pending.json").exists()
    assert (
        json.loads(tmp_path.joinpath("state/staging-web.json").read_text())["config"]
        == GIT.value
    )


def test_deployer_recovers_pending_desired_commit_without_up(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    asyncio.run(deployer.deploy(deploy_operation()))
    state_path = tmp_path / "state" / "staging-web.json"
    pending_path = tmp_path / "state" / "staging-web.pending.json"
    desired = json.loads(state_path.read_text())
    pending_path.write_text(json.dumps({"previous": None, "desired": desired}))
    up_count = sum("up" in call[0] for call in runner.calls)

    asyncio.run(deployer.deploy(deploy_operation()))

    assert sum("up" in call[0] for call in runner.calls) == up_count
    assert not pending_path.exists()


def test_deployer_recovers_pending_previous_and_retries_desired(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    asyncio.run(deployer.deploy(deploy_operation()))
    state_path = tmp_path / "state" / "staging-web.json"
    pending_path = tmp_path / "state" / "staging-web.pending.json"
    previous = json.loads(state_path.read_text())
    desired = {
        "image": IMAGE_REF.value,
        "config": GIT_CONFIG.value,
        "config_digest": "sha256:" + "c" * 64,
    }
    pending_path.write_text(json.dumps({"previous": previous, "desired": desired}))

    asyncio.run(
        deployer.deploy(
            DeployImageOperation(
                image_input=IMAGE_REF,
                target="staging",
                config_revision=GIT_CONFIG,
                deployment_name="web",
            )
        )
    )

    assert not pending_path.exists()
    assert json.loads(state_path.read_text())["config"] == GIT_CONFIG.value
    up_calls = [call for call in runner.calls if "up" in call[0]]
    assert up_calls[-2][2]["AGENTD_CONFIG_REVISION"] == GIT.value
    assert up_calls[-1][2]["AGENTD_CONFIG_REVISION"] == GIT_CONFIG.value


def test_deployer_recovers_pending_initial_by_compose_down(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    deployer = compose_deployer(tmp_path, runner)
    pending_path = tmp_path / "state" / "staging-web.pending.json"
    compose = asyncio.run(
        deployer._prepare_compose_file(deployer.targets["staging"], GIT)
    )
    environment = deployer._environment(
        deployer.targets["staging"], IMAGE_REF.value, GIT.value, "web"
    )
    config_digest = asyncio.run(
        deployer._resolve_config(
            compose,
            "web",
            environment,
            expected_image=IMAGE_REF.value,
            expected_config=GIT.value,
        )
    )
    desired = {
        "image": IMAGE_REF.value,
        "config": GIT.value,
        "config_digest": config_digest,
    }
    pending_path.write_text(json.dumps({"previous": None, "desired": desired}))

    asyncio.run(deployer.deploy(deploy_operation()))

    down_calls = [call for call in runner.calls if "down" in call[0]]
    assert len(down_calls) == 1
    assert "--volumes" not in down_calls[0][0]
    assert not pending_path.exists()


def test_deployer_rollback_failure_leaves_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailRollbackRunner(FakeCommandRunner):
        up_count = 0
        fail_after = 2

        async def run(self, argv, *, cwd=None, environment=None, timeout=None):
            if "up" in tuple(argv):
                self.up_count += 1
                if self.up_count > self.fail_after:
                    raise OperationError("injected rollback failure")
            return await super().run(
                argv,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
            )

    runner = FailRollbackRunner()
    deployer = compose_deployer(tmp_path, runner)
    asyncio.run(deployer.deploy(deploy_operation()))
    changed = DeployImageOperation(
        image_input=IMAGE_REF,
        target="staging",
        config_revision=GIT_CONFIG,
        deployment_name="web",
    )

    real_fsync = operations.os.fsync
    fsyncs = 0

    def fail_applied_state_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        # Pending file fsync, pending directory fsync, then applied file fsync.
        if fsyncs == 3:
            raise OSError("injected applied-state fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(operations.os, "fsync", fail_applied_state_fsync)
    with pytest.raises(OperationError, match="rollback failed"):
        asyncio.run(deployer.deploy(changed))

    assert (tmp_path / "state" / "staging-web.pending.json").exists()


def test_operation_driver_runs_typed_operation_and_cancels(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    builder = DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner)
    deployer = compose_deployer(tmp_path, runner)
    driver = OperationHarnessDriver(builder, deployer)
    handle = asyncio.run(driver.start(contract(deploy_operation())))
    result = asyncio.run(driver.collect(handle))
    assert result.outcome is RunOutcome.COMPLETED
    with pytest.raises(OperationError):
        asyncio.run(driver.start(contract(None)))
    asyncio.run(driver.cancel(handle))


def test_operation_driver_status_reports_terminal_and_converts_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner = FakeCommandRunner()
        driver = OperationHarnessDriver(
            DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner),
            compose_deployer(tmp_path, runner),
            repository_allowlist={"https://github.com/acme/app.git"},
        )
        handle = await driver.start(contract(deploy_operation()))
        status = await driver.status(handle)
        assert status["known"] is True
        result = await driver.collect(handle)
        assert result.outcome is RunOutcome.COMPLETED
        status = await driver.status(handle)
        assert status["terminal"] is True
        assert status["result"]["outcome"] == RunOutcome.COMPLETED.value

        forbidden = BuildImageOperation(
            source_input=GIT,
            source_repository="https://github.com/evil/app.git",
            output_name="image",
            registry_repository=IMAGE,
        )
        failed_handle = await driver.start(contract(forbidden))
        failed = await driver.collect(failed_handle)
        assert failed.outcome is RunOutcome.FAILED
        assert "error_type" in failed.metadata
        failed_status = await driver.status(failed_handle)
        assert failed_status["terminal"] is True
        assert failed_status["result"]["outcome"] == RunOutcome.FAILED.value

    asyncio.run(scenario())


def test_operation_driver_enforces_repository_allowlist(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    driver = OperationHarnessDriver(
        DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner),
        compose_deployer(tmp_path, runner, targets={}),
        repository_allowlist={"https://github.com/acme/app.git"},
    )
    forbidden = BuildImageOperation(
        source_input=GIT,
        source_repository="https://github.com/evil/app.git",
        output_name="image",
        registry_repository=IMAGE,
    )
    handle = asyncio.run(driver.start(contract(forbidden)))
    result = asyncio.run(driver.collect(handle))
    assert result.outcome is RunOutcome.FAILED
    assert result.metadata["error_type"] == "OperationError"


def test_operation_driver_serializes_identical_builds_and_pushes_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner = FakeCommandRunner()
        driver = OperationHarnessDriver(
            DockerImageBuilder(frozenset({IMAGE}), tmp_path / "cache", runner),
            compose_deployer(tmp_path, runner, targets={}),
        )
        execution = contract(build_operation())

        first, second = await asyncio.gather(
            driver.start(execution),
            driver.start(execution),
        )
        results = await asyncio.gather(
            driver.collect(first),
            driver.collect(second),
        )

        assert all(result.outcome is RunOutcome.COMPLETED for result in results)
        pushes = [
            args
            for args, _cwd, _environment in runner.calls
            if args[:4] == ("docker", "buildx", "build", "--push")
        ]
        assert len(pushes) == 1
        assert driver._operation_locks == {}

    asyncio.run(scenario())
