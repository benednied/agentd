from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import agentd.bootstrap as bootstrap
from agentd.config import ServiceConfig


def test_runtime_pins_goldenage_dev_toolchain_and_codex_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provisioner_arguments: dict[str, object] = {}
    client_arguments: list[dict[str, object]] = []

    class RecordingProvisioner:
        def __init__(self, **kwargs) -> None:
            provisioner_arguments.update(kwargs)

        async def prepare(self, _lease) -> None:
            raise AssertionError("bootstrap test must not provision a repository")

    class RecordingClient:
        def __init__(self, **kwargs) -> None:
            client_arguments.append(kwargs)

    monkeypatch.setattr(bootstrap, "TrustedUvProvisioner", RecordingProvisioner)
    monkeypatch.setattr(bootstrap, "OpenAICodexClient", RecordingClient)
    config = ServiceConfig(
        database=tmp_path / "state" / "state.sqlite",
        workspace_root=tmp_path / "workspaces",
        codex_home=tmp_path / "codex-home",
        uv_cache=tmp_path / "uv-cache",
    )

    runtime = bootstrap.create_local_runtime(
        config.database,
        config.workspace_root,
        include_fake_driver=False,
        include_codex_cli_driver=False,
        trusted_provisioning=True,
        config=config,
    )
    try:
        assert provisioner_arguments["python_install_directory"] == (
            config.uv_cache / "python"
        )
        assert provisioner_arguments["python_version"] == "3.14"
        assert provisioner_arguments["extras"] == ("dev",)
        assert runtime.supervisor is not None
        runtime.supervisor._client_factory(
            SimpleNamespace(working_directory=str(tmp_path / "leased-worktree"))
        )
        assert runtime.account_oracle is not None
        runtime.account_oracle._client_factory()
        expected_environment = {
            "CODEX_HOME": str(config.codex_home),
            "UV_CACHE_DIR": str(config.uv_cache),
            "UV_PYTHON_INSTALL_DIR": str(config.uv_cache / "python"),
            "UV_PYTHON_PREFERENCE": "only-managed",
        }
        assert client_arguments == [
            {
                "environment": expected_environment,
                "cwd": str(tmp_path / "leased-worktree"),
            },
            {"environment": expected_environment},
        ]
    finally:
        runtime.close()
