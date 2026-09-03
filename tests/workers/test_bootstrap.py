from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import ClassVar

import pytest

from agentd.cli import build_parser
from agentd.workers import (
    OperationAllowlist,
    OperationHarnessDriver,
    WorkerServeConfig,
    create_worker_server,
    run_worker_server,
)


def _values(tmp_path: Path, *, tls: bool = False) -> dict[str, str]:
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  app:\n    image: alpine\n")
    values = {
        "AGENTD_WORKER_HOST": "127.0.0.1",
        "AGENTD_WORKER_PORT": "0",
        "AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK": "1",
        "AGENTD_WORKER_NODE_ID": "node-1",
        "AGENTD_WORKER_SESSION_EPOCH": "epoch-1",
        "AGENTD_WORKER_JOURNAL": str(tmp_path / "worker.sqlite3"),
        "AGENTD_WORKER_PSK": "p" * 32,
        "AGENTD_WORKER_REPOSITORIES": "/srv/repos/example",
        "AGENTD_WORKER_REGISTRIES": "registry.example/team/app",
        "AGENTD_WORKER_COMPOSE_TARGETS": json.dumps(
            {
                "staging": {
                    "source_repository": "/srv/repos/example",
                    "compose_file": "compose.yml",
                    "environment": {"FIXED_CONFIG": "yes"},
                }
            }
        ),
    }
    if tls:
        values["AGENTD_WORKER_TLS_CERT"] = str(tmp_path / "cert.pem")
        values["AGENTD_WORKER_TLS_KEY"] = str(tmp_path / "key.pem")
    return values


def test_worker_serve_config_requires_identity_journal_secret_and_tls() -> None:
    with pytest.raises(ValueError, match="node_id"):
        WorkerServeConfig.from_environment({})

    values = {
        "AGENTD_WORKER_HOST": "127.0.0.1",
        "AGENTD_WORKER_NODE_ID": "node-1",
        "AGENTD_WORKER_SESSION_EPOCH": "epoch-1",
        "AGENTD_WORKER_JOURNAL": "/tmp/worker.sqlite3",
    }
    with pytest.raises(ValueError, match="PSK"):
        WorkerServeConfig.from_environment(values)
    values["AGENTD_WORKER_PSK"] = "p" * 32
    with pytest.raises(ValueError, match="TLS certificate"):
        WorkerServeConfig.from_environment(values)

    values["AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK"] = "true"
    config = WorkerServeConfig.from_environment(values)
    assert config.tls_context() is None


def test_secret_can_only_be_loaded_from_env_or_mode_0600_file(tmp_path: Path) -> None:
    values = _values(tmp_path)
    config = WorkerServeConfig.from_environment(values)
    assert config.load_secret(values) == b"p" * 32

    secret_file = tmp_path / "worker.psk"
    secret_file.write_bytes(b"f" * 32)
    os.chmod(secret_file, stat.S_IRUSR | stat.S_IWUSR)
    file_values = {
        key: value for key, value in values.items() if key != "AGENTD_WORKER_PSK"
    }
    file_values["AGENTD_WORKER_PSK_FILE"] = str(secret_file)
    file_config = WorkerServeConfig.from_environment(file_values)
    assert file_config.load_secret(file_values) == b"f" * 32

    os.chmod(secret_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ValueError, match="0600"):
        file_config.load_secret(file_values)

    both = dict(values, AGENTD_WORKER_PSK_FILE=str(secret_file))
    with pytest.raises(ValueError, match="env or file"):
        WorkerServeConfig.from_environment(both)


def test_operation_allowlist_is_strict_for_json_and_environment(
    tmp_path: Path,
) -> None:
    values = _values(tmp_path)
    config_file = tmp_path / "operations.json"
    config_file.write_text(
        json.dumps(
            {
                "repositories": ["/srv/repos/example"],
                "registries": ["registry.example/team/app"],
                "compose_targets": {
                    "staging": {
                        "source_repository": "/srv/repos/example",
                        "compose_file": "compose.yml",
                        "environment": {},
                    }
                },
            }
        )
    )
    values.pop("AGENTD_WORKER_REPOSITORIES")
    values.pop("AGENTD_WORKER_REGISTRIES")
    values.pop("AGENTD_WORKER_COMPOSE_TARGETS")
    values["AGENTD_WORKER_OPERATIONS_CONFIG"] = str(config_file)
    config = WorkerServeConfig.from_environment(values)
    allowlist = config.operation_allowlist(values)
    assert allowlist.repositories == frozenset({"/srv/repos/example"})
    assert allowlist.registries == frozenset({"registry.example/team/app"})
    assert tuple(allowlist.compose_targets) == ("staging",)

    bad = dict(values)
    bad_config = tmp_path / "bad.json"
    bad_config.write_text(
        json.dumps(
            {
                "repositories": ["/srv/repos/example"],
                "registries": ["registry.example/team/app"],
                "compose_targets": {},
                "unexpected": True,
            }
        )
    )
    bad["AGENTD_WORKER_OPERATIONS_CONFIG"] = str(bad_config)
    with pytest.raises(ValueError, match="exactly"):
        WorkerServeConfig.from_environment(bad).operation_allowlist(bad)

    ssh_values = dict(values)
    ssh_values.pop("AGENTD_WORKER_OPERATIONS_CONFIG")
    ssh_values["AGENTD_WORKER_REPOSITORIES"] = "git@host.example:team/repo"
    with pytest.raises(ValueError, match="SSH/SCP"):
        OperationAllowlist.from_environment(ssh_values)

    https_values = _values(tmp_path)
    https_values["AGENTD_WORKER_REPOSITORIES"] = (
        f"https://git.example/team/repo,file://{tmp_path / 'local-repo'}"
    )
    https_values["AGENTD_WORKER_COMPOSE_TARGETS"] = json.dumps(
        {
            "staging": {
                "source_repository": "https://git.example/team/repo",
                "compose_file": "compose.yml",
                "environment": {},
            }
        }
    )
    accepted = OperationAllowlist.from_environment(https_values)
    assert accepted.repositories == frozenset(
        {
            "https://git.example/team/repo",
            f"file://{tmp_path / 'local-repo'}",
        }
    )

    http_values = dict(https_values)
    http_values["AGENTD_WORKER_REPOSITORIES"] = "http://git.example/team/repo"
    with pytest.raises(ValueError, match="HTTPS or a local file"):
        OperationAllowlist.from_environment(http_values)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"repositories": [], "repositories": [], "registries": [], '
        '"compose_targets": {}}'
    )
    duplicate_values = dict(values)
    duplicate_values["AGENTD_WORKER_OPERATIONS_CONFIG"] = str(duplicate)
    with pytest.raises(ValueError, match="valid JSON"):
        OperationAllowlist.from_environment(duplicate_values)


def test_create_worker_server_registers_only_typed_operations_driver(
    tmp_path: Path,
) -> None:
    values = _values(tmp_path)
    config = WorkerServeConfig.from_environment(values)
    server = create_worker_server(config, values=values)
    try:
        driver = server._service._drivers.get("operations")
        assert isinstance(driver, OperationHarnessDriver)
        assert config.journal_path.exists()
        assert (config.operation_cache_root or tmp_path / "cache").is_dir()
    finally:
        asyncio.run(server.close())


def test_worker_psk_environment_is_scrubbed_before_operation_subprocesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_name = "AGENTD_TEST_WORKER_SECRET"
    monkeypatch.setenv(secret_name, "s" * 32)
    values = _values(tmp_path)
    values.pop("AGENTD_WORKER_PSK")
    values["AGENTD_WORKER_PSK_ENV"] = secret_name
    values[secret_name] = "s" * 32
    config = WorkerServeConfig.from_environment(values)

    server = create_worker_server(config, values=values)
    try:
        assert secret_name not in os.environ
    finally:
        asyncio.run(server.close())


def test_worker_server_signal_shutdown_is_composable_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _values(tmp_path)
    config = WorkerServeConfig.from_environment(values)

    class FakeServer:
        instances: ClassVar[list[FakeServer]] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.closed = False
            self.started = False
            self.__class__.instances.append(self)

        async def start(self) -> tuple[str, int]:
            self.started = True
            return "127.0.0.1", 1234

        async def serve_forever(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("agentd.workers.bootstrap.WorkerServer", FakeServer)

    stop = asyncio.Event()
    stop.set()
    assert asyncio.run(run_worker_server(config, values=values, stop=stop)) == 0
    assert FakeServer.instances[0].started is True
    assert FakeServer.instances[0].closed is True


def test_worker_serve_parser_has_no_secret_argument() -> None:
    args = build_parser().parse_args(
        [
            "worker-serve",
            "--node-id",
            "node-1",
            "--session-epoch",
            "epoch-1",
            "--journal",
            "/tmp/worker.sqlite3",
        ]
    )
    assert args.command == "worker-serve"
    assert not hasattr(args, "psk")
