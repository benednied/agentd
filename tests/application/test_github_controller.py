"""Exercise the administrative CLI and the actual controller composition."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentd.cli import main
from agentd.coding.controller import create_controller, load_config, status
from agentd.coding.models import RepositoryProfile
from agentd.domain.enums import QuotaUnit
from agentd.domain.models import ProviderQuotaSnapshot, QuotaPool
from agentd.intake.models import SourceIssue
from agentd.state.sqlite import SQLiteStateStore


@pytest.fixture
def controller_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentd.cli.configure_logging", lambda **kwargs: None)
    issue = SourceIssue(
        "test/repo",
        7,
        1,
        "I_1",
        "Make change",
        "Intent",
        "2026-09-20T12:00:00Z",
        labels=("agentd:approved",),
    )

    class Source:
        current = issue
        polls = 0

        def get(self, repository, number):
            assert repository == "test/repo" and number == 1
            return self.current

        def poll(self, repository):
            self.polls += 1
            return (self.current,)

    source = Source()
    monkeypatch.setattr("agentd.coding.controller.GitHubIssueSource", lambda: source)
    profile = RepositoryProfile(
        "repo",
        "1",
        "test/repo",
        "https://github.com/test/repo.git",
        validation_commands=(("git", "diff", "--check"),),
    )
    config = {
        "profile": profile.to_dict(),
        "repository_id": 7,
        "base_commit": "a" * 40,
        "base_branch": "master",
        "database": "controller.sqlite",
        "object_cache": "objects",
        "unused_workspace_root": "unused",
        "account_pool": "account",
        "expected_tokens": 100,
        "maximum_tokens": 200,
        "quota_command": [sys.executable, "-c", "print('{}')"],
        "worker": {
            "name": "remote",
            "host": "127.0.0.1",
            "port": 54321,
            "node_id": "worker",
            "session_epoch": "epoch",
            "psk_file": "secret",
            "allow_insecure_loopback": True,
        },
    }
    secret = tmp_path / "secret"
    secret.write_bytes(b"q" * 32)
    secret.chmod(0o600)
    path = tmp_path / "controller.json"
    path.write_text(json.dumps(config))
    return path, source


def test_approve_status_and_restart_preserve_one_job_and_accounting(
    controller_config, capsys
):
    path, source = controller_config
    arguments = ["github", "--config", str(path)]
    assert main([*arguments, "approve", "1", "--actor", "operator"]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["approved_revision"] == source.current.revision
    assert main([*arguments, "approve", "1", "--actor", "operator"]) == 0
    capsys.readouterr()
    config = load_config(path)
    with SQLiteStateStore(config["database"]) as store:
        store.register_quota_pool(
            QuotaPool(
                "account", "codex", 77, reserved=11, debt=4, unit=QuotaUnit.TOKENS
            )
        )

    async def scenario():
        for _ in range(2):
            runtime = create_controller(config)
            try:
                await runtime.intake.poll()
                assert len(runtime.store.list_jobs()) == 1
                assert not runtime.store.list_runs()
                pool = runtime.store.get_quota_pool("account")
                assert (pool.remaining, pool.reserved, pool.debt) == (77, 11, 4)
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    # Status and approval do not need a reachable worker or its credentials.
    Path(config["worker"]["psk_file"]).unlink()
    assert main([*arguments, "status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["job_id"] == approved["job_id"]
    assert output[0]["state"] == "READY"
    assert output[0]["authorized"]
    assert "Intent" not in json.dumps(output)


def test_controller_discovers_but_never_approves_issues(controller_config):
    path, source = controller_config

    async def scenario():
        runtime = create_controller(load_config(path))
        try:
            await runtime.intake.poll()
            assert source.polls == 1
            assert not runtime.store.list_jobs()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_edited_issue_loses_authority_and_cannot_be_dispatched(
    controller_config, capsys
):
    path, source = controller_config
    main(["github", "--config", str(path), "approve", "1", "--actor", "operator"])
    capsys.readouterr()
    source.current = replace(source.current, body="Changed after approval")

    async def scenario():
        runtime = create_controller(load_config(path))
        try:
            await runtime.intake.poll()
            assert not status(runtime.store)[0]["authorized"]
            assert not runtime.store.list_runs()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_oracle_rejects_observation_from_wrong_pool(controller_config):
    from agentd.coding.controller import AdministrativeOracle

    path, _ = controller_config
    config = load_config(path)
    snapshot = ProviderQuotaSnapshot(
        pool_id="unrelated", bucket_id="account", primary_used_percent=1
    )
    command = [sys.executable, "-c", f"print({json.dumps(snapshot.to_dict())!r})"]
    with SQLiteStateStore(config["database"]) as store:
        oracle = AdministrativeOracle(command, store, "account")
        with pytest.raises(ValueError, match="different account"):
            asyncio.run(oracle.snapshot())
        assert not store.list_provider_quota_snapshots("unrelated")


@pytest.mark.parametrize(
    "change",
    [
        {"state": "closed"},
        {"labels": ()},
        {"body": "Edited"},
        {"repository_id": 8},
        {"node_id": "I_replacement"},
    ],
)
def test_publication_refresh_rejects_changed_authority(controller_config, change):
    from agentd.coding.controller import create_intake

    path, source = controller_config
    config = load_config(path)
    with SQLiteStateStore(config["database"]) as store:
        intake = create_intake(config, store)
        approved = intake.approve("test/repo", 1, actor="operator")
        intake.refresh_authorization(approved)
        source.current = replace(approved, **change)
        with pytest.raises(ValueError):
            intake.refresh_authorization(approved)
