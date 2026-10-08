from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from agentd.domain.models import WorkspaceLease
from agentd.provisioning import (
    RepositoryProvisioningError,
    TrustedUvProvisioner,
    _run_command,
)


def _lease(workspace: Path) -> WorkspaceLease:
    return WorkspaceLease(
        id="lease-1",
        job_id="job-1",
        repository=str(workspace),
        branch="agentd/job-1",
        working_directory=str(workspace),
        base_ref="HEAD",
    )


def test_provisioner_hides_codex_home_and_scrubs_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    codex_home = tmp_path / "codex-home"
    state_directory = tmp_path / "state"
    cache = tmp_path / "uv-cache"
    provision_home = tmp_path / "provision-home"
    python_install = cache / "python"
    observed: list[dict[str, object]] = []

    async def runner(arguments, cwd, environment):
        observed.append(
            {
                "arguments": tuple(arguments),
                "cwd": cwd,
                "environment": environment,
            }
        )
        return 0, b"", b""

    monkeypatch.setenv("CODEX_HOME", "/secret/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    provisioner = TrustedUvProvisioner(
        codex_home=codex_home,
        state_directory=state_directory,
        cache_directory=cache,
        provisioning_home=provision_home,
        python_install_directory=python_install,
        python_version="3.14",
        extras=("dev",),
        runner=runner,
    )

    asyncio.run(provisioner.prepare(_lease(workspace)))

    assert len(observed) == 2
    install_arguments = observed[0]["arguments"]
    sync_arguments = observed[1]["arguments"]
    install_environment = observed[0]["environment"]
    sync_environment = observed[1]["environment"]
    assert isinstance(install_arguments, tuple)
    assert isinstance(sync_arguments, tuple)
    assert install_arguments[:3] == (
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
    )
    assert "--unshare-user" in install_arguments
    assert "--unshare-user" in sync_arguments
    proc_index = install_arguments.index("--ro-bind", 10)
    assert install_arguments[proc_index : proc_index + 3] == (
        "--ro-bind",
        "/proc",
        "/proc",
    )
    tmpfs_index = install_arguments.index("--tmpfs")
    assert install_arguments[tmpfs_index : tmpfs_index + 2] == (
        "--tmpfs",
        str(codex_home),
    )
    assert install_arguments[tmpfs_index + 2 : tmpfs_index + 4] == (
        "--tmpfs",
        str(state_directory),
    )
    assert install_arguments[-5:] == ("--", "uv", "python", "install", "3.14")
    assert sync_arguments[-8:] == (
        "--",
        "uv",
        "sync",
        "--frozen",
        "--extra",
        "dev",
        "--python",
        "3.14",
    )
    assert str(cache) in install_arguments
    assert str(provision_home) in install_arguments
    assert str(cache) not in sync_arguments
    assert str(provision_home) not in sync_arguments
    assert all(call["cwd"] == workspace for call in observed)
    assert isinstance(install_environment, dict)
    assert install_environment["HOME"] == str(provision_home)
    assert install_environment["UV_CACHE_DIR"] == str(cache)
    assert install_environment["UV_PYTHON_INSTALL_DIR"] == str(python_install)
    assert install_environment["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert "CODEX_HOME" not in install_environment
    assert "OPENAI_API_KEY" not in install_environment
    assert isinstance(sync_environment, dict)
    assert sync_environment["HOME"] == str(workspace / ".uv-cache" / "home")
    assert sync_environment["UV_CACHE_DIR"] == str(workspace / ".uv-cache")
    assert sync_environment["UV_PYTHON_INSTALL_DIR"] == str(python_install)
    assert sync_environment["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert sync_environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert "CODEX_HOME" not in sync_environment
    assert "OPENAI_API_KEY" not in sync_environment


def test_provisioner_surfaces_failure_without_secret_output(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    calls = 0

    async def runner(_arguments, _cwd, _environment):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0, b"", b""
        return 2, b"ignored stdout", b"locked resolution failed"

    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
        runner=runner,
    )

    with pytest.raises(
        RepositoryProvisioningError,
        match="locked resolution failed",
    ):
        asyncio.run(provisioner.prepare(_lease(workspace)))


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ('[dependency-groups]\ndev = ["pytest"]\n', ("--group", "dev")),
        ('[project.optional-dependencies]\ndev = ["pytest"]\n', ("--extra", "dev")),
        ('[project]\nname = "minimal"\n', ()),
    ],
)
def test_development_dependencies_follow_repository_metadata(
    tmp_path: Path, metadata: str, expected: tuple[str, ...]
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(metadata)
    commands: list[tuple[str, ...]] = []

    async def runner(arguments, _cwd, _environment):
        commands.append(tuple(arguments))
        return 0, b"", b""

    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
        development_dependencies=True,
        runner=runner,
    )
    asyncio.run(provisioner.prepare(_lease(workspace)))
    command = commands[-1]
    assert command[command.index("uv") :] == (
        "uv",
        "sync",
        "--frozen",
        *expected,
        "--python",
        "3.14",
    )


def test_provisioner_rejects_python_install_outside_dedicated_cache(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
        python_install_directory=tmp_path / "outside-cache" / "python",
    )

    with pytest.raises(
        RepositoryProvisioningError,
        match="inside the dedicated uv cache",
    ):
        asyncio.run(provisioner.prepare(_lease(workspace)))


def test_provisioner_rejects_missing_workspace(tmp_path: Path) -> None:
    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
    )

    with pytest.raises(RepositoryProvisioningError, match="does not exist"):
        asyncio.run(provisioner.prepare(_lease(tmp_path / "missing")))


@pytest.mark.parametrize("python_pin", ["3.12", "3.12.12"])
def test_repository_python_pin_overrides_fallback(
    tmp_path: Path, python_pin: str
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".python-version").write_text(python_pin + "\n")
    commands: list[tuple[str, ...]] = []

    async def runner(arguments, _cwd, _environment):
        commands.append(tuple(arguments))
        return 0, b"", b""

    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
        runner=runner,
    )
    asyncio.run(provisioner.prepare(_lease(workspace)))
    assert commands[0][-3:] == ("python", "install", python_pin)
    assert commands[1][-2:] == ("--python", python_pin)


def test_provisioner_rejects_option_like_repository_python_pin(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".python-version").write_text("--system")
    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
    )
    with pytest.raises(RepositoryProvisioningError, match="numeric Python version"):
        asyncio.run(provisioner.prepare(_lease(workspace)))


def test_command_runner_kills_and_reaps_process_group_when_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        communicating = asyncio.Event()

        class FakeProcess:
            pid = 4242
            returncode = -signal.SIGKILL
            waited = False

            async def communicate(self):
                communicating.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def wait(self):
                self.waited = True
                return self.returncode

        process = FakeProcess()

        async def create_subprocess_exec(*_arguments, **kwargs):
            assert kwargs["start_new_session"] is True
            return process

        killed: list[tuple[int, signal.Signals]] = []
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            create_subprocess_exec,
        )
        monkeypatch.setattr(
            "agentd.provisioning.os.killpg",
            lambda pid, sig: killed.append((pid, sig)),
        )

        task = asyncio.create_task(_run_command(("uv",), tmp_path, {}))
        await communicating.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert killed == [(process.pid, signal.SIGKILL)]
        assert process.waited

    asyncio.run(scenario())


def test_repository_build_state_is_private_per_lease(tmp_path: Path) -> None:
    cache = tmp_path / "shared-uv-cache"
    python_install = cache / "python"
    sync_environments: list[dict[str, str]] = []

    async def runner(arguments, _cwd, environment):
        if "sync" in arguments:
            sync_environments.append(dict(environment))
        return 0, b"", b""

    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=cache,
        provisioning_home=tmp_path / "provision-home",
        python_install_directory=python_install,
        runner=runner,
    )
    first_workspace = tmp_path / "worktree-1"
    second_workspace = tmp_path / "worktree-2"
    first_workspace.mkdir()
    second_workspace.mkdir()

    asyncio.run(provisioner.prepare(_lease(first_workspace)))
    asyncio.run(provisioner.prepare(_lease(second_workspace)))

    assert [env["UV_CACHE_DIR"] for env in sync_environments] == [
        str(first_workspace / ".uv-cache"),
        str(second_workspace / ".uv-cache"),
    ]
    assert [env["HOME"] for env in sync_environments] == [
        str(first_workspace / ".uv-cache" / "home"),
        str(second_workspace / ".uv-cache" / "home"),
    ]
    assert all(
        env["UV_PYTHON_INSTALL_DIR"] == str(python_install)
        for env in sync_environments
    )
    assert all(env["UV_PYTHON_DOWNLOADS"] == "never" for env in sync_environments)


def test_provisioner_rejects_symlinked_private_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (workspace / ".uv-cache").symlink_to(target, target_is_directory=True)
    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
    )

    with pytest.raises(RepositoryProvisioningError, match="cannot be a symbolic link"):
        asyncio.run(provisioner.prepare(_lease(workspace)))
