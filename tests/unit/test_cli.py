import asyncio
import json
import os
from pathlib import Path

import agentd.cli as cli
from agentd.cli import build_parser, main
from agentd.config import ServiceConfig
from agentd.domain.enums import QuotaUnit
from agentd.domain.models import UsageSample
from agentd.state.sqlite import SQLiteStateStore


def test_submit_persists_codex_cumulative_maximum(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "state.sqlite"

    assert (
        main(
            [
                "--db",
                str(database),
                "submit",
                "--project",
                "goldenage",
                "--repository",
                "/srv/goldenage",
                "--base-ref",
                "f360390442659908c6a4b740b988c76b1dddef6c",
                "--objective",
                "replace deprecated startup hooks",
                "--p50",
                "100",
                "--p90",
                "135",
                "--quota",
                "100000",
                "--quota-maximum",
                "150000",
                "--quota-pool",
                "codex",
                "--harness",
                "codex",
            ]
        )
        == 0
    )
    capsys.readouterr()

    with SQLiteStateStore(database) as store:
        job = store.list_jobs()[0]
    assert job.quota_budget.maximum == 150_000
    assert job.quota_budget.unit is QuotaUnit.TOKENS
    assert job.preferred_model_class == "gpt-5.6-terra"
    assert job.minimum_model_class == "gpt-5.6-terra"
    assert job.base_ref == "f360390442659908c6a4b740b988c76b1dddef6c"


def test_register_quota_accepts_token_unit(tmp_path: Path, capsys) -> None:
    database = tmp_path / "state.sqlite"

    assert (
        main(
            [
                "--db",
                str(database),
                "register-quota",
                "codex",
                "--provider",
                "chatgpt",
                "--remaining",
                "150000",
                "--unit",
                "tokens",
            ]
        )
        == 0
    )
    capsys.readouterr()

    with SQLiteStateStore(database) as store:
        pool = store.get_quota_pool("codex")
    assert pool.unit is QuotaUnit.TOKENS


def test_usage_command_prints_run_ledger(tmp_path: Path, capsys) -> None:
    database = tmp_path / "state.sqlite"
    sample = UsageSample(
        id="sample-1",
        run_id="run-1",
        thread_id="thread-1",
        turn_id="turn-1",
        sequence=1,
        cumulative_quota=42,
        unit=QuotaUnit.TOKENS,
    )
    with SQLiteStateStore(database) as store:
        # Usage ownership requires a real run in production. This parser/query
        # test only needs an empty ledger for an unknown run.
        assert store.list_usage_samples(sample.run_id) == []

    assert main(["--db", str(database), "usage", "--run", "run-1"]) == 0
    output = capsys.readouterr().out
    assert '"run-1": []' in output


def test_codex_status_defaults_to_most_restrictive_bucket() -> None:
    parser = build_parser()

    automatic = parser.parse_args(["codex-status"])
    explicit = parser.parse_args(["codex-status", "--bucket", "codex"])

    assert automatic.bucket is None
    assert explicit.bucket == "codex"


def test_serve_passes_validated_service_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed = []

    async def serve(config):
        observed.append(config)
        return 0

    monkeypatch.setattr(cli, "_serve_service", serve)
    database = tmp_path / "state.sqlite"
    workspaces = tmp_path / "workspaces"
    codex_home = tmp_path / "codex-home"

    assert (
        main(
            [
                "--db",
                str(database),
                "--workspace-root",
                str(workspaces),
                "--codex-home",
                str(codex_home),
                "serve",
            ]
        )
        == 0
    )
    assert observed[0].database == database
    assert observed[0].workspace_root == workspaces
    assert observed[0].codex_home == codex_home


def test_serve_service_composes_remote_workers_and_scrubs_transport_psk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "remote-workers.json"
    config_path.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "name": "remote-a",
                        "host": "127.0.0.1",
                        "port": 8765,
                        "node_id": "node-a",
                        "session_epoch": "epoch-a",
                        "psk_env": "AGENTD_TEST_REMOTE_PSK",
                        "allow_insecure_loopback": True,
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("AGENTD_REMOTE_WORKERS_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTD_TEST_REMOTE_PSK", "p" * 32)
    observed: dict[str, object] = {}

    class FakeRuntime:
        control_plane = object()
        account_oracle = None

        async def aclose(self) -> None:
            observed["closed"] = True

    def create_runtime(*_args, **kwargs):
        observed["runtime_kwargs"] = kwargs
        observed["secret_at_composition"] = os.environ.get("AGENTD_TEST_REMOTE_PSK")
        return FakeRuntime()

    class FakeDaemon:
        def __init__(self, _plane, **kwargs) -> None:
            observed["daemon_kwargs"] = kwargs

        async def serve(self, _stop: asyncio.Event) -> None:
            observed["served"] = True

    monkeypatch.setattr("agentd.bootstrap.create_local_runtime", create_runtime)
    monkeypatch.setattr("agentd.daemon.AgentDaemon", FakeDaemon)
    config = ServiceConfig.from_environment(
        {
            "HOME": str(tmp_path),
            "AGENTD_DB": str(tmp_path / "state.sqlite"),
            "AGENTD_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        }
    )

    assert asyncio.run(cli._serve_service(config)) == 0

    runtime_kwargs = observed["runtime_kwargs"]
    assert isinstance(runtime_kwargs, dict)
    assert len(runtime_kwargs["worker_backends"]) == 1
    assert len(runtime_kwargs["additional_drivers"]) == 1
    assert runtime_kwargs["additional_drivers"][0].capabilities().name == "operations"
    assert observed["secret_at_composition"] is None
    assert observed["served"] is True
    assert observed["closed"] is True
