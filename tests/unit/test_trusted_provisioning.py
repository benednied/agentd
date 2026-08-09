from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.domain.models import WorkspaceLease
from agentd.provisioning import RepositoryProvisioningError, TrustedUvProvisioner


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
    observed: dict[str, object] = {}

    async def runner(arguments, cwd, environment):
        observed.update(arguments=tuple(arguments), cwd=cwd, environment=environment)
        return 0, b"", b""

    monkeypatch.setenv("CODEX_HOME", "/secret/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    provisioner = TrustedUvProvisioner(
        codex_home=codex_home,
        state_directory=state_directory,
        cache_directory=cache,
        provisioning_home=provision_home,
        extras=("dev",),
        runner=runner,
    )

    asyncio.run(provisioner.prepare(_lease(workspace)))

    arguments = observed["arguments"]
    environment = observed["environment"]
    assert isinstance(arguments, tuple)
    assert arguments[:3] == ("/usr/bin/bwrap", "--die-with-parent", "--new-session")
    tmpfs_index = arguments.index("--tmpfs")
    assert arguments[tmpfs_index : tmpfs_index + 2] == ("--tmpfs", str(codex_home))
    assert arguments[tmpfs_index + 2 : tmpfs_index + 4] == (
        "--tmpfs",
        str(state_directory),
    )
    assert arguments[-6:] == ("--", "uv", "sync", "--frozen", "--extra", "dev")
    assert observed["cwd"] == workspace
    assert isinstance(environment, dict)
    assert environment["HOME"] == str(provision_home)
    assert environment["UV_CACHE_DIR"] == str(cache)
    assert "CODEX_HOME" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_provisioner_surfaces_failure_without_secret_output(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    async def runner(_arguments, _cwd, _environment):
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


def test_provisioner_rejects_missing_workspace(tmp_path: Path) -> None:
    provisioner = TrustedUvProvisioner(
        codex_home=tmp_path / "codex-home",
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        provisioning_home=tmp_path / "home",
    )

    with pytest.raises(RepositoryProvisioningError, match="does not exist"):
        asyncio.run(provisioner.prepare(_lease(tmp_path / "missing")))
