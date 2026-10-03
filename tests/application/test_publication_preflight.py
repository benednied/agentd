"""Publication preparation blockers remain safe and visible across restarts."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from agentd.coding.attempts import CodingAttemptLimits
from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.controller import status
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome, RunState
from agentd.domain.models import EffortEstimate, QuotaBudget, RunResult, TokenUsage
from agentd.domain.transitions import transition_job
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.workflow import GitHubStatusReporter
from agentd.publication import PublicationError, PublicationStore
from agentd.state.sqlite import SQLiteStateStore


@pytest.fixture
def rig(tmp_path, monkeypatch):
    store = SQLiteStateStore(tmp_path / "state.sqlite")
    issue = SourceIssue(
        "owner/repo", 42, 7, "I_7", "Fix a bug", "Intent", "2026-10-03T10:00:00Z"
    )
    policy = IntakePolicy("owner/repo", 42, eligibility_label=None)
    store.observe_github_issue(issue, policy)
    store.approve_github_issue(issue, policy, actor="trusted-fixture")
    profile = RepositoryProfile(
        "test",
        "v1",
        issue.repository,
        "https://github.com/owner/repo.git",
        validation_commands=(("true",),),
    )
    compiler = CodingJobCompiler(
        profile,
        "b" * 40,
        QuotaBudget(10, maximum=20, unit=QuotaUnit.TOKENS),
        EffortEstimate(1, 2),
    )
    job = store.create_github_job(issue, compiler(issue))
    for state in (JobState.ADMITTED, JobState.RUNNING, JobState.REVIEW):
        updated, event = transition_job(job, state, "trusted test execution")
        store.save_job(updated, event, expected=job)
        job = updated
    run = SimpleNamespace(
        id="run-1",
        node_id="worker",
        state=RunState.COMPLETED,
        contract=SimpleNamespace(operation=job.operation),
        result=RunResult(
            RunOutcome.COMPLETED,
            commit="c" * 40,
            usage=TokenUsage(input_tokens=12),
            metadata={"coding_evidence": {"profile_digest": profile.digest}},
        ),
    )
    store.latest_run = lambda _job_id: run
    ledger = PublicationStore(store.path)
    calls = []

    def publish(intent, collected, repository, *, authorization_check):
        calls.append(intent)
        collected.verify(intent)
        authorization_check()
        ledger.bind(intent)
        ledger.save(
            intent, "published", pr={"url": "https://github.com/owner/repo/pull/8"}
        )
        return {"url": "https://github.com/owner/repo/pull/8"}

    publisher = SimpleNamespace(store=ledger, publish=publish)
    publications = CodingPublicationReconciler(
        store,
        publisher,
        {profile.id: profile},
        {issue.repository: tmp_path / "cache"},
        {issue.repository: "master"},
    )
    monkeypatch.setattr(
        "agentd.coding.pipeline.import_coding_bundle", lambda *args: None
    )
    yield SimpleNamespace(
        store=store,
        ledger=ledger,
        publisher=publisher,
        publications=publications,
        job=job,
        run=run,
        calls=calls,
    )
    store.close()


def test_preflight_failure_survives_restart_then_retries_the_same_result(
    rig, monkeypatch
):
    def unavailable(*_args):
        raise PublicationError("Trusted Git operation failed")

    monkeypatch.setattr("agentd.coding.pipeline.ensure_authorized_base", unavailable)
    original_job, original_result = rig.job, rig.run.result
    report = asyncio.run(rig.publications.reconcile())[0]
    assert report["preflight"] == {
        "status": "blocked",
        "reason": "Trusted Git operation failed",
        "error_class": "PublicationError",
    }
    assert rig.ledger.get(rig.job.id) is None
    assert rig.calls == []
    restarted = PublicationStore(rig.store.path)
    assert restarted.preflight_for(rig.job.id, rig.run.id) == report["preflight"]
    blocked = status(rig.store)[0]
    assert blocked["blocked_reason"] == (
        "Trusted Git operation failed; retrying publication automatically"
    )
    reporter = GitHubStatusReporter(rig.store, SimpleNamespace())
    reporter.enqueue([blocked])
    assert blocked["blocked_reason"] in rig.store.pending_github_status()[0]["body"]
    rig.publisher.store = restarted
    monkeypatch.setattr(
        "agentd.coding.pipeline.ensure_authorized_base", lambda *_args: None
    )
    assert asyncio.run(rig.publications.reconcile()) == (
        {"url": "https://github.com/owner/repo/pull/8"},
    )
    assert restarted.preflight_for(rig.job.id, rig.run.id) is None
    assert restarted.get(rig.job.id)["stage"] == "published"
    delivered = status(rig.store)[0]
    assert delivered["blocked_reason"] is None
    reporter.enqueue([delivered])
    assert (
        "Trusted Git operation failed"
        not in (rig.store.pending_github_status()[0]["body"])
    )
    assert rig.store.get_job(rig.job.id) == original_job
    assert rig.run.result == original_result
    assert len(rig.calls) == 1 and rig.calls[0].run_id == rig.run.id


def test_status_reports_preparation_repair_without_a_publication_candidate(rig):
    failed, event = transition_job(rig.job, JobState.FAILED, "Preparation failed")
    rig.store.save_job(failed, event, expected=rig.job)
    rig.run.backend = "remote"
    rig.run.driver = "remote-coding"
    rig.run.state = RunState.FAILED
    rig.run.result = RunResult(
        RunOutcome.FAILED,
        usage=TokenUsage(),
        consumed_quota=0,
        metadata={
            "telemetry_valid": True,
            "provider_started": False,
            "preparation_failure": True,
        },
    )
    rig.store.list_runs = lambda _job_id: (rig.run,)
    reason = "Coding repair preparation attempt limit has been reached"
    rig.ledger.record_repair(rig.job.id, rig.run.id, "exhausted", reason)
    assert rig.ledger.get(rig.job.id) is None
    limits = CodingAttemptLimits(maximum_preparation_attempts=1)
    report = status(rig.store, attempt_limits=limits)[0]
    assert report["repair"] == {"status": "exhausted", "reason": reason}
    assert report["attempts"] == 1
    assert report["attempt_budget"]["preparation_attempts"] == 1
    assert report["attempt_budget"]["coding_attempts"] == 0
    reporter = GitHubStatusReporter(rig.store, SimpleNamespace())
    reporter.enqueue([report])
    assert reason in rig.store.pending_github_status()[0]["body"]


def test_suspended_status_reports_exhausted_coding_attempt_limit(rig):
    failed, event = transition_job(rig.job, JobState.FAILED, "Stopped execution")
    rig.store.save_job(failed, event, expected=rig.job)
    suspended, event = transition_job(failed, JobState.SUSPENDED, "Retained checkpoint")
    rig.store.save_job(suspended, event, expected=failed)
    rig.run.backend = "remote"
    rig.run.driver = "remote-coding"
    rig.run.state = RunState.SUSPENDED
    rig.store.list_runs = lambda _job_id: (rig.run,)
    report = status(
        rig.store, attempt_limits=CodingAttemptLimits(maximum_coding_attempts=1)
    )[0]
    assert report["repair"] == {
        "status": "exhausted",
        "reason": "Coding repair provider attempt limit has been reached",
    }
    assert report["attempt_budget"]["coding_attempts"] == 1


@pytest.mark.parametrize(
    "error, expected_class",
    [
        (
            PublicationError("https://secret:token@github.com/repo.git"),
            "PublicationError",
        ),
        (OSError("credential stderr ghp_secret"), "OSError"),
        (KeyError("model-selected-profile-secret"), "KeyError"),
        (
            type("SecretException", (Exception,), {})("private output"),
            "PublicationFailure",
        ),
    ],
)
def test_unknown_preflight_errors_never_persist_raw_diagnostics(
    rig, monkeypatch, error, expected_class
):
    def failed(*_args):
        raise error

    monkeypatch.setattr("agentd.coding.pipeline.ensure_authorized_base", failed)
    with pytest.raises(type(error)):
        rig.publications.publish_job(rig.job.id)
    outcome = PublicationStore(rig.store.path).preflight_for(rig.job.id, rig.run.id)
    assert outcome == {
        "status": "blocked",
        "reason": "Trusted publication preflight failed",
        "error_class": expected_class,
    }
    with sqlite3.connect(rig.store.path) as database:
        assert database.execute(
            "SELECT reason,error_class FROM publication_preflight_errors"
        ).fetchall() == [(outcome["reason"], expected_class)]
    reporter = GitHubStatusReporter(rig.store, SimpleNamespace())
    reporter.enqueue(status(rig.store))
    assert (
        "Waiting: Trusted publication preflight failed"
        in (rig.store.pending_github_status()[0]["body"])
    )
    assert str(error) not in rig.store.pending_github_status()[0]["body"]


def test_known_error_whitelist_requires_exact_message_and_run_identity(rig):
    rig.ledger.record_preflight_error(
        rig.job.id,
        rig.run.id,
        PublicationError("Trusted Git operation failed\ncredential ghp_secret"),
    )
    assert rig.ledger.preflight_for(rig.job.id, rig.run.id)["reason"] == (
        "Trusted publication preflight failed"
    )
    assert rig.ledger.preflight_for(rig.job.id, "new-run") is None
    assert rig.ledger.preflight_for("another-job", rig.run.id) is None


def test_bound_candidate_takes_precedence_over_later_publication_error(
    rig, monkeypatch
):
    monkeypatch.setattr(
        "agentd.coding.pipeline.ensure_authorized_base", lambda *_args: None
    )
    original = rig.publisher.publish

    def failed_after_bind(intent, *args, **kwargs):
        original(intent, *args, **kwargs)
        raise OSError("private push acknowledgement output")

    rig.publisher.publish = failed_after_bind
    report = asyncio.run(rig.publications.reconcile())[0]
    assert report["preflight"] is None
    assert rig.ledger.preflight_for(rig.job.id, rig.run.id) is None
    assert rig.ledger.get(rig.job.id)["stage"] == "published"
