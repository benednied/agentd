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
    assert output[0]["attempt_budget"] == {
        "coding_attempts": 0,
        "preparation_attempts": 0,
        "total_attempts": 0,
        "maximum_coding_attempts": 3,
        "maximum_preparation_attempts": 3,
        "maximum_total_attempts": 6,
    }
    assert "Intent" not in json.dumps(output)


def test_status_cli_reports_configured_separate_attempt_limits(
    controller_config, capsys
):
    path, _ = controller_config
    config = json.loads(path.read_text())
    config.update(
        {
            "maximum_automatic_attempts": 5,
            "maximum_preparation_attempts": 2,
            "maximum_total_attempts": 6,
        }
    )
    path.write_text(json.dumps(config))
    arguments = ["github", "--config", str(path)]
    assert main([*arguments, "approve", "1", "--actor", "operator"]) == 0
    capsys.readouterr()
    assert main([*arguments, "status"]) == 0
    budget = json.loads(capsys.readouterr().out)[0]["attempt_budget"]
    assert budget["maximum_coding_attempts"] == 5
    assert budget["maximum_preparation_attempts"] == 2
    assert budget["maximum_total_attempts"] == 6


@pytest.mark.parametrize("prior_attempts, blocked", [(1, False), (2, True)])
def test_configured_github_resume_cli_enforces_attempt_limits(
    controller_config, monkeypatch, capsys, prior_attempts, blocked
):
    from types import SimpleNamespace

    from agentd.coordinator import LifecycleError
    from agentd.domain.enums import JobState
    from agentd.domain.transitions import transition_job

    path, _source = controller_config
    config = json.loads(path.read_text())
    config["maximum_automatic_attempts"] = 2
    path.write_text(json.dumps(config))
    arguments = ["github", "--config", str(path)]
    assert main([*arguments, "approve", "1", "--actor", "operator"]) == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    config = load_config(path)
    with SQLiteStateStore(config["database"]) as store:
        job = store.get_job(job_id)
        for state in (JobState.ADMITTED, JobState.RUNNING, JobState.SUSPENDED):
            updated, event = transition_job(job, state, "Retained execution")
            store.save_job(updated, event, expected=job)
            job = updated
        runs = tuple(
            SimpleNamespace(
                id=f"prior-{index}",
                result=None,
                contract=SimpleNamespace(operation=None),
            )
            for index in range(prior_attempts)
        )
        store.list_runs = lambda _job_id: runs
        store.latest_run = lambda _job_id: runs[-1]
        resumed = []

        async def resume(job_id, **_kwargs):
            resumed.append(job_id)
            return job

        async def close():
            pass

        runtime = SimpleNamespace(
            store=store, coordinator=SimpleNamespace(resume=resume), aclose=close
        )
        monkeypatch.setattr(
            "agentd.coding.controller.create_controller", lambda _config: runtime
        )
        command = [*arguments, "resume", job_id, "--actor", "operator"]
        if blocked:
            with pytest.raises(LifecycleError, match="provider attempt limit"):
                main(command)
            assert resumed == []
        else:
            assert main(command) == 0
            assert resumed == [job_id]


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


def test_standing_owner_policy_wires_automatic_intake_without_a_label_or_cli(
    controller_config, monkeypatch
):
    path, source = controller_config
    source.current = replace(
        source.current,
        labels=(),
        author_login="owner",
        author_id=123,
        created_at="2026-09-20T12:00:00Z",
        material_updated_at="2026-09-20T12:00:00Z",
    )
    source.issue_authority = lambda issue: (issue, (("owner", 123), ("owner", 123)))
    source.comments = lambda *_args, **_kwargs: ()
    source.reviews = lambda *_args, **_kwargs: ()
    monkeypatch.setattr("agentd.coding.controller.GitHubWorkflowSource", lambda: source)
    config = load_config(path)
    config.update(
        {
            "eligibility_label": None,
            "standing_github_policy": {
                "trusted_actors": {"owner": 123},
                "activated_at": "2026-09-20T11:00:00Z",
            },
        }
    )

    async def scenario():
        runtime = create_controller(config)
        try:
            assert runtime.workflow is not None
            assert runtime.intake.source is source
            await runtime.intake.poll()
            jobs = runtime.store.list_jobs()
            assert len(jobs) == 1
            assert jobs[0].quota_budget.maximum == 200
            assert runtime.store.github_source_for_job(jobs[0].id)[
                "approved_by"
            ].startswith("github-policy:")
            assert not runtime.store.list_runs()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_controller_liveness_survives_expected_quota_wait_but_stale_polling_is_dead(
    controller_config, monkeypatch
):
    from datetime import timedelta

    from agentd.coding.controller import health
    from agentd.domain.models import (
        ResourceVector,
        WorkerHeartbeat,
        WorkerNode,
        utc_now,
    )
    from agentd.runtime.health import RuntimeHealthStore

    path, _source = controller_config
    config = load_config(path)
    config["standing_github_policy"] = {
        "trusted_actors": {"owner": 123},
        "activated_at": "2026-09-20T11:00:00Z",
    }
    monkeypatch.setattr(
        "agentd.coding.controller.ControllerLock.held", lambda _self: True
    )
    now = utc_now()
    monkeypatch.setattr("agentd.coding.controller.utc_now", lambda: now)
    with SQLiteStateStore(config["database"]) as store:
        store.register_quota_pool(QuotaPool("account", "codex", 200))
        store.register_node(
            WorkerNode(
                "worker",
                labels={},
                capacity=ResourceVector(1, 1),
                harnesses=frozenset({"remote-coding"}),
                heartbeat=WorkerHeartbeat(
                    "epoch", frozenset({"remote-coding"}), 0, observed_at=now
                ),
            )
        )
        store.append_provider_quota_snapshot(
            ProviderQuotaSnapshot(
                "account", "pool", primary_used_percent=80, observed_at=now
            )
        )
        with RuntimeHealthStore(config["database"]) as pulses:
            pulses.pulse("controller", state="waiting_quota", at=now)
            pulses.pulse("publisher", state="waiting_review", at=now)
            pulses.pulse("source", at=now)
        blocked = health(config, store)
        assert blocked["controller_live"]
        assert blocked["liveness"]["controller"]["expected_wait"]
        assert blocked["source_fresh"]
        assert blocked["workers"][0]["fresh"]
        assert not blocked["admission_telemetry_ready"]
        assert blocked["provider_wait_reason"] == "quota_provider_pressure"
        later = now + timedelta(seconds=601)
        monkeypatch.setattr("agentd.coding.controller.utc_now", lambda: later)
        stale = health(config, store)
        assert not stale["controller_live"]
        assert stale["liveness"]["controller"]["reason"] == "heartbeat_stale"


@pytest.mark.parametrize("primary,secondary", [(90, 20), (20, 90), (90, 90)])
def test_reserve_policy_is_shared_by_admission_and_active_governor(primary, secondary):
    from agentd.coding.controller import coding_provider_policies
    from agentd.domain.models import utc_now
    from agentd.runtime.accounts import unattended_provider_wait_reason
    from agentd.runtime.governor import provider_stop_command

    account, stop = coding_provider_policies({"provider_reserve_percent": 10})
    now = utc_now()
    snapshot = ProviderQuotaSnapshot(
        pool_id="account",
        bucket_id="codex",
        observed_at=now,
        primary_used_percent=primary,
        secondary_used_percent=secondary,
    )
    assert unattended_provider_wait_reason(snapshot, at=now, policy=account) == (
        "quota_provider_pressure"
    )
    assert (
        provider_stop_command(
            snapshot, run_id="run", at=now, policy=stop, account_policy=account
        ).action
        == "interrupt"
    )
    below = replace(snapshot, primary_used_percent=89.9, secondary_used_percent=89.9)
    assert unattended_provider_wait_reason(below, at=now, policy=account) is None
    assert (
        provider_stop_command(
            below, run_id="run", at=now, policy=stop, account_policy=account
        )
        is None
    )


@pytest.mark.parametrize("reserve", [True, "10", -1, 1, 100, float("nan")])
def test_invalid_provider_reserve_is_rejected(reserve):
    from agentd.coding.controller import coding_provider_policies

    with pytest.raises(ValueError):
        coding_provider_policies({"provider_reserve_percent": reserve})


@pytest.mark.parametrize(
    "changes",
    [
        {"secondary_used_percent": None},
        {"primary_used_percent": None},
        {"confidence": 0},
        {"age": 301},
        {"age": -60},
    ],
)
def test_reserve_policy_stops_when_telemetry_cannot_protect_both_windows(changes):
    from datetime import timedelta

    from agentd.coding.controller import coding_provider_policies
    from agentd.domain.models import utc_now
    from agentd.runtime.accounts import unattended_provider_wait_reason
    from agentd.runtime.governor import provider_stop_command

    changes = dict(changes)
    now = utc_now()
    age = changes.pop("age", 0)
    snapshot = ProviderQuotaSnapshot(
        pool_id="account",
        bucket_id="codex",
        observed_at=now - timedelta(seconds=age),
        **({"primary_used_percent": 20, "secondary_used_percent": 20} | changes),
    )
    account, stop = coding_provider_policies({"provider_reserve_percent": 10})
    assert unattended_provider_wait_reason(snapshot, at=now, policy=account) in {
        "quota_unknown",
        "quota_stale",
    }
    command = provider_stop_command(
        snapshot, run_id="run", at=now, policy=stop, account_policy=account
    )
    assert command.action == "interrupt"
    assert command.payload["reason"] == "provider reserve telemetry is unavailable"


def test_status_reuses_snapshots_per_pool_only_within_each_pass(
    controller_config, monkeypatch
):
    from datetime import UTC, datetime

    path, source = controller_config
    config = load_config(path)

    async def scenario():
        runtime = create_controller(config)
        try:
            monkeypatch.setattr(
                source, "get", lambda repository, number: source.current
            )
            for number in range(1, 31):
                source.current = replace(
                    source.current, number=number, node_id=f"I_{number}"
                )
                runtime.intake.approve("test/repo", number, actor="operator")
                await runtime.intake.poll()
            store = runtime.store
            assert len(store.list_jobs()) == 30
            calls = []
            original = store.latest_provider_quota_snapshot

            def counted_latest(pool_id, bucket_id=None):
                calls.append((pool_id, bucket_id))
                return original(pool_id, bucket_id)

            monkeypatch.setattr(store, "latest_provider_quota_snapshot", counted_latest)
            reports = status(store)
            assert len(reports) == 30
            assert calls == [("account", None)]
            assert all(r["quota"]["provider_observed_at"] is None for r in reports)
            snapshot = ProviderQuotaSnapshot(
                id="fresh",
                pool_id="account",
                bucket_id="codex",
                observed_at=datetime.now(UTC),
            )
            store.append_provider_quota_snapshot(snapshot)
            reports = status(store)
            assert calls == [("account", None), ("account", None)]
            assert all(
                r["quota"]["provider_observed_at"] == snapshot.observed_at.isoformat()
                for r in reports
            )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
