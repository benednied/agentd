"""An authenticated approval authorizes exactly one delivered, checked PR head."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.integration import GitHubIntegrationReconciler, IntegrationBlocked
from agentd.coding.models import RepositoryProfile
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome, RunState
from agentd.domain.models import EffortEstimate, QuotaBudget, RunResult, TokenUsage
from agentd.domain.transitions import transition_job
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.workflow import _instant
from agentd.publication import PublicationIntent, PublicationStore
from agentd.state.sqlite import SQLiteStateStore

HEAD = "a" * 40
MERGE = "b" * 40


class GitHub:
    def __init__(self):
        self.pr = {
            "number": 8,
            "node_id": "PR_8",
            "state": "open",
            "draft": True,
            "merged": False,
            "mergeable": True,
            "html_url": "https://github.com/owner/repo/pull/8",
            "head": {"sha": HEAD, "repo": {"id": 42}},
            "base": {"ref": "master", "repo": {"id": 42}},
        }
        self.review_data = []
        self.check_data = [
            {
                "name": "quality",
                "head_sha": HEAD,
                "app": {"slug": "github-actions"},
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.merges = 0
        self.readies = 0
        self.lose_merge_response = False
        self.after_ready = lambda: None
        self.after_merge = lambda: None
        self.requested_heads = []
        self.repository_id = 42

    def repository(self, repository):
        assert repository == "owner/repo"
        return {"id": self.repository_id}

    def pull_request(self, repository, number):
        assert (repository, number) == ("owner/repo", 8)
        return self.pr

    def reviews(self, repository, number):
        return tuple(self.review_data)

    def check_runs(self, repository, commit):
        assert commit == HEAD
        return tuple(self.check_data)

    def ready(self, node_id):
        assert node_id == "PR_8"
        self.readies += 1
        self.pr["draft"] = False
        self.after_ready()

    def merge(self, repository, number, *, commit, method):
        self.requested_heads.append(commit)
        assert commit == self.pr["head"]["sha"]
        assert method == "merge"
        self.merges += 1
        self.pr.update(
            {
                "state": "closed",
                "merged": True,
                "merge_commit_sha": MERGE,
                "merged_by": {"login": "owner", "id": 123},
            }
        )
        self.after_merge()
        if self.lose_merge_response:
            raise TimeoutError("merge accepted; response lost")
        return {"merged": True, "sha": MERGE}


@pytest.fixture
def rig(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite")
    issue = SourceIssue(
        "owner/repo",
        42,
        7,
        "I_7",
        "Fix a bug",
        "Expected behavior",
        "2026-10-03T10:00:00Z",
    )
    policy = IntakePolicy("owner/repo", 42, eligibility_label=None)
    store.observe_github_issue(issue, policy)
    store.approve_github_issue(issue, policy, actor="trusted-fixture")
    profile = RepositoryProfile(
        "test",
        "v1",
        "owner/repo",
        "https://github.com/owner/repo.git",
        validation_commands=(("true",),),
    )
    compiler = CodingJobCompiler(
        profile,
        "c" * 40,
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
        state=RunState.COMPLETED,
        result=RunResult(
            RunOutcome.COMPLETED,
            commit=HEAD,
            consumed_quota=12,
            usage=TokenUsage(input_tokens=12),
            metadata={
                "telemetry_valid": True,
                "coding_evidence": {"result_commit": HEAD},
            },
        ),
    )
    store.latest_run = lambda _job_id: run
    ledger = PublicationStore(store.path)
    intent = PublicationIntent(
        job.id,
        issue.repository,
        issue.number,
        issue.revision,
        "master",
        "c" * 40,
        HEAD,
        "worker",
        run.id,
        "v1",
        (("true",),),
    )
    ledger.bind(intent)
    ledger.save(
        intent,
        "published",
        evidence=[{"returncode": 0}],
        pr={"url": "https://github.com/owner/repo/pull/8"},
    )
    adapter = GitHub()
    refreshes = []
    config = {
        "profile": profile.to_dict(),
        "repository_id": 42,
        "base_branch": "master",
        "standing_github_policy": {
            "trusted_actors": {"owner": 123},
            "activated_at": "2026-10-03T09:00:00Z",
        },
        "integration": {
            "enabled": True,
            "required_checks": ["quality"],
            "merge_method": "merge",
        },
    }
    publications = SimpleNamespace(publisher=SimpleNamespace(store=ledger))
    integration = GitHubIntegrationReconciler(
        store, publications, config, source_refresh=refreshes.append, adapter=adapter
    )
    when = (
        _instant(ledger.get(job.id)["delivery"]["completed_at"]) + timedelta(seconds=1)
    ).isoformat()
    value = SimpleNamespace(
        store=store,
        issue=issue,
        policy=policy,
        run=run,
        job=job,
        ledger=ledger,
        adapter=adapter,
        config=config,
        integration=integration,
        when=when,
        refreshes=refreshes,
        publications=publications,
    )
    yield value
    store.close()


def approve(rig, **kwargs):
    asyncio.run(
        rig.integration.approve_pr(
            rig.job.id,
            actor="github:owner:123",
            event_id="approval-1",
            occurred_at=rig.when,
            **kwargs,
        )
    )


def test_comment_approval_is_db_only_and_publisher_integrates_one_exact_checked_head(
    rig,
):
    approve(rig)
    assert rig.adapter.merges == rig.adapter.readies == 0
    assert rig.refreshes == []
    report = asyncio.run(rig.integration.reconcile())[0]
    assert report["integration_stage"] == "merged"
    assert rig.adapter.requested_heads == [HEAD]
    assert rig.adapter.merges == rig.adapter.readies == 1
    assert rig.store.get_job(rig.job.id).state is JobState.COMPLETED
    assert rig.refreshes == [rig.issue]
    assert asyncio.run(rig.integration.reconcile()) == ()


def test_no_merge_without_human_approval_even_when_model_and_checks_say_success(rig):
    report = asyncio.run(rig.integration.reconcile())[0]
    assert "human approval" in report["integration_wait_reason"]
    assert rig.adapter.merges == rig.adapter.readies == 0


@pytest.mark.parametrize(
    "actor", ["github:owner:999", "github:stranger:123", "owner", "github:owner:True"]
)
def test_approval_requires_immutable_trusted_actor_identity(rig, actor):
    with pytest.raises(IntegrationBlocked, match="actor"):
        asyncio.run(
            rig.integration.approve_pr(
                rig.job.id, actor=actor, event_id="bad", occurred_at=rig.when
            )
        )
    assert rig.adapter.merges == 0


def test_approval_cannot_be_reused_for_a_future_candidate_or_different_head(rig):
    with pytest.raises(IntegrationBlocked, match="predates"):
        asyncio.run(
            rig.integration.approve_pr(
                rig.job.id,
                actor="github:owner:123",
                event_id="old",
                occurred_at="2026-10-03T09:00:00Z",
            )
        )
    with pytest.raises(IntegrationBlocked, match="head"):
        approve(rig, head_commit="d" * 40)
    approve(rig, head_commit=HEAD)
    rig.run.id = "run-2"
    assert (
        "latest metered"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == 0


@pytest.mark.parametrize(
    "change",
    [
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"head_sha": "d" * 40},
        {"app": {"slug": "untrusted-app"}},
        {"name": "other"},
    ],
)
def test_required_checks_must_be_successful_github_actions_on_exact_head(rig, change):
    approve(rig)
    rig.adapter.check_data[0].update(change)
    assert (
        "exact head"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == rig.adapter.readies == 0


def test_ready_pr_head_race_blocks_before_merge_cas(rig):
    approve(rig)
    rig.adapter.after_ready = lambda: rig.adapter.pr["head"].update({"sha": "d" * 40})
    assert (
        "live PR"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == 0


def test_recreated_repository_or_changed_base_cannot_integrate(rig):
    approve(rig)
    rig.adapter.repository_id = 99
    assert (
        "repository identity"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    rig.adapter.repository_id = 42
    rig.adapter.pr["base"]["ref"] = "attacker"
    assert (
        "live PR"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == 0


def test_source_revocation_after_successful_checks_blocks_external_effects(rig):
    approve(rig)
    rig.integration.source_refresh = lambda issue: rig.store.observe_github_issue(
        replace(issue, state="closed"), rig.policy
    )
    assert (
        "source authorization"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == rig.adapter.readies == 0


def test_hold_or_pending_feedback_prevents_integration_even_after_approval(rig):
    approve(rig)
    rig.store.set_github_job_hold(rig.job.id, held=True, event_id="pause")
    assert (
        "paused"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    rig.store.set_github_job_hold(rig.job.id, held=False, event_id="resume")
    rig.store.record_github_control("feedback", 8, {"body": "Please add a test"})
    assert (
        "feedback"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == 0


def test_lost_merge_response_and_issue_autoclosure_reconcile_without_duplicate_merge(
    rig,
):
    approve(rig)
    rig.adapter.lose_merge_response = True
    rig.adapter.after_merge = lambda: rig.store.observe_github_issue(
        replace(rig.issue, state="closed"), rig.policy
    )
    assert (
        asyncio.run(rig.integration.reconcile())[0]["integration_stage"]
        == "merge_pending"
    )
    reopened = GitHubIntegrationReconciler(
        rig.store,
        rig.publications,
        rig.config,
        source_refresh=rig.refreshes.append,
        adapter=rig.adapter,
    )
    assert asyncio.run(reopened.reconcile())[0]["integration_stage"] == "merged"
    assert rig.adapter.merges == 1
    assert rig.store.get_job(rig.job.id).state is JobState.COMPLETED


def test_native_approval_requires_latest_trusted_review_on_exact_commit(rig):
    rig.adapter.review_data = [
        {
            "id": 1,
            "state": "APPROVED",
            "commit_id": "d" * 40,
            "user": {"login": "owner", "id": 123},
            "submitted_at": rig.when,
        }
    ]
    assert (
        "human approval"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    rig.adapter.review_data[0]["commit_id"] = HEAD
    assert asyncio.run(rig.integration.reconcile())[0]["integration_stage"] == "merged"


def test_dismissed_native_review_and_later_change_request_revoke_approval(rig):
    approve(rig)
    rig.adapter.review_data = [
        {
            "id": 2,
            "state": "CHANGES_REQUESTED",
            "commit_id": HEAD,
            "user": {"login": "owner", "id": 123},
            "submitted_at": (_instant(rig.when) + timedelta(seconds=1)).isoformat(),
        }
    ]
    assert (
        "human approval"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.adapter.merges == 0


def test_invalid_terminal_usage_or_unresolved_worker_ownership_blocks_grant(rig):
    rig.run.result = replace(
        rig.run.result,
        usage=None,
        metadata={"telemetry_valid": False, "coding_evidence": {}},
    )
    with pytest.raises(IntegrationBlocked, match="terminal ownership and usage"):
        approve(rig)
    assert rig.adapter.merges == 0


def test_merge_history_without_trusted_merger_is_not_accepted(rig):
    rig.adapter.pr.update(
        {
            "merged": True,
            "merge_commit_sha": MERGE,
            "merged_by": {"login": "stranger", "id": 999},
        }
    )
    assert (
        "trusted integration actor"
        in asyncio.run(rig.integration.reconcile())[0]["integration_wait_reason"]
    )
    assert rig.store.get_job(rig.job.id).state is JobState.REVIEW


def test_integration_grant_is_replayed_identically_but_cannot_be_rebound(rig):
    approve(rig)
    approve(rig)
    with pytest.raises(IntegrationBlocked, match="rebound"):
        rig.integration._grant(
            "approval-1",
            rig.job.id,
            "run-2",
            "d" * 40,
            "owner",
            123,
            rig.when,
            "comment",
        )
