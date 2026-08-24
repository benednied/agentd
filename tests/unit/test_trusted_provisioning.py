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
    environment = observed[0]["environment"]
    assert observed[1]["environment"] == environment
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
    assert all(call["cwd"] == workspace for call in observed)
    assert isinstance(environment, dict)
    assert environment["HOME"] == str(provision_home)
    assert environment["UV_CACHE_DIR"] == str(cache)
    assert environment["UV_PYTHON_INSTALL_DIR"] == str(python_install)
    assert environment["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert "CODEX_HOME" not in environment
    assert "OPENAI_API_KEY" not in environment


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
