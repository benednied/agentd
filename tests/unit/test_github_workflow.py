"""Standing approval, durable controls, and publication response ambiguity."""

import asyncio
from dataclasses import replace

import pytest

from agentd.domain.enums import JobState, QoSClass
from agentd.domain.models import EffortEstimate, Job, QuotaBudget
from agentd.domain.transitions import transition_job
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.service import GitHubIntake
from agentd.intake.workflow import (
    GitHubComment,
    GitHubStatusRejected,
    GitHubStatusReporter,
    GitHubWorkflow,
    GitHubWorkflowSource,
    StandingGitHubPolicy,
    parse_control,
)
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore


def source_issue(**changes):
    return replace(
        SourceIssue(
            "owner/repo",
            42,
            7,
            "I_7",
            "Fix the bug",
            "The specified behavior fails.",
            "2026-10-03T10:05:00Z",
            author_login="owner",
            author_id=123,
            created_at="2026-10-03T10:05:00Z",
            material_updated_at="2026-10-03T10:05:00Z",
        ),
        **changes,
    )


def source_comment(body, *, identity=1, author="owner", author_id=123, number=7):
    return GitHubComment(
        f"IC_{identity}",
        identity,
        number,
        author,
        author_id,
        body,
        "2026-10-03T10:06:00Z",
        "2026-10-03T10:06:00Z",
    )


def compile_job(issue):
    return Job(
        id=issue.job_id,
        project=issue.repository,
        repository=f"https://github.com/{issue.repository}.git",
        objective=issue.title + "\n\n" + issue.body,
        qos=QoSClass.SCAVENGER,
        quota_budget=QuotaBudget(10, maximum=20),
        effort=EffortEstimate(1, 2),
    )


class Source:
    def __init__(self, issue):
        self.issue = issue
        self.controls = []
        self.actors = ((issue.author_login, issue.author_id),) * 2
        self.comment_editors = {}
        self.scans = []

    def get(self, repository, number):
        assert repository == self.issue.repository
        assert number == self.issue.number
        return self.issue

    def poll(self, repository):
        return (self.issue,)

    def issue_authority(self, issue):
        assert issue.revision == self.issue.revision
        return self.issue, self.actors

    def comments(self, repository, *, since, kind):
        self.scans.append((since, kind))
        return tuple(comment for comment in self.controls if comment.kind == kind)

    def verify_comment(self, comment):
        return (
            (comment.author_login, comment.author_id),
            self.comment_editors.get(
                comment.identity, (comment.author_login, comment.author_id)
            ),
        )

    def reviews(self, repository, number):
        return ()


def compose(store, source, **workflow_options):
    intake = GitHubIntake(
        store,
        source,
        (IntakePolicy("owner/repo", 42, eligibility_label=None),),
        compile_job,
        ControlPlane(store),
    )

    async def cancel(job_id):
        job = store.get_job(job_id)
        changed, event = transition_job(job, JobState.CANCELLED, "GitHub control")
        store.save_job(changed, event, expected=job)
        return changed

    intake.control_plane.cancel = cancel
    workflow = GitHubWorkflow(
        intake,
        source,
        StandingGitHubPolicy("owner/repo", 42, {"owner": 123}, "2026-10-03T10:00:00Z"),
        **workflow_options,
    )
    intake.workflow = workflow
    return intake, workflow


def test_new_trusted_issue_is_approved_without_label_or_local_cli_and_replays(tmp_path):
    path = tmp_path / "state.sqlite"
    issue = source_issue()
    source = Source(issue)
    for _ in range(2):
        with SQLiteStateStore(path) as store:
            intake, _ = compose(store, source)
            asyncio.run(intake.poll())
            assert len(store.list_jobs()) == 1
            job = store.get_job(issue.job_id)
            assert job.state is JobState.READY
            assert job.quota_budget.maximum == 20
            assert job.qos is QoSClass.SCAVENGER
            row = store.github_source_for_job(job.id)
            assert row["approved_revision"] == issue.revision
            assert row["approved_by"].startswith("github-policy:")
            approvals = store._connection.execute(
                "SELECT COUNT(*) FROM github_source_events WHERE action='approved'"
            ).fetchone()[0]
            assert approvals == 1


@pytest.mark.parametrize(
    "change",
    [
        {"author_login": "stranger", "author_id": 999},
        {"author_login": "owner", "author_id": 999},
        {"created_at": "2026-10-03T09:59:00Z"},
        {"is_pull_request": True},
        {"state": "closed"},
    ],
)
def test_untrusted_or_preexisting_sources_do_not_get_standing_approval(change):
    with SQLiteStateStore() as store:
        intake, _ = compose(store, Source(source_issue(**change)))
        asyncio.run(intake.poll())
        assert store.list_jobs() == []


def test_latest_untrusted_editor_cannot_use_original_trusted_author_to_get_approval():
    source = Source(source_issue())
    source.actors = (("owner", 123), ("stranger", 999))
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.list_jobs() == []


def test_trusted_prelaunch_edit_rebinds_only_exact_current_revision():
    source = Source(source_issue())
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        first = store.get_job(source.issue.job_id)
        source.issue = replace(source.issue, body="Different approved behavior")
        asyncio.run(intake.poll())
        revised = store.get_job(source.issue.job_id)
        assert revised.id == first.id
        assert revised.objective.endswith(source.issue.body)
        assert (
            store.github_source_for_job(first.id)["approved_revision"]
            == source.issue.revision
        )


def test_plain_trusted_comment_is_durable_task_intent_and_cannot_change_policy(
    tmp_path,
):
    path = tmp_path / "state.sqlite"
    source = Source(source_issue())
    source.controls = [
        source_comment("Use the alternate wording; maximum_tokens=999999")
    ]
    for _ in range(2):
        with SQLiteStateStore(path) as store:
            intake, _ = compose(store, source)
            asyncio.run(intake.poll())
            job = store.get_job(source.issue.job_id)
            assert job.objective.count("Use the alternate wording") == 1
            assert job.quota_budget.maximum == 20
            assert len(store.github_controls("applied")) == 1


@pytest.mark.parametrize(
    "comment,editor",
    [
        (source_comment("/agentd cancel", author="stranger", author_id=999), None),
        (source_comment("/agentd cancel", author_id=999), None),
        (source_comment("/agentd cancel"), ("stranger", 999)),
    ],
)
def test_untrusted_comment_or_comment_editor_never_controls_job(comment, editor):
    source = Source(source_issue())
    source.controls = [comment]
    if editor:
        source.comment_editors[comment.identity] = editor
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.get_job(source.issue.job_id).state is JobState.READY
        assert not store.github_job_held(source.issue.job_id)
        assert store.github_controls() == []


def test_pause_is_durable_resume_clears_hold_and_edited_comment_does_not_reexecute(
    tmp_path,
):
    path = tmp_path / "state.sqlite"
    source = Source(source_issue())
    source.controls = [source_comment("/agentd pause")]
    with SQLiteStateStore(path) as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.github_job_held(source.issue.job_id)
    source.controls = [
        source_comment("/agentd cancel"),
        source_comment("/agentd resume", identity=2),
    ]
    with SQLiteStateStore(path) as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert not store.github_job_held(source.issue.job_id)
        assert store.get_job(source.issue.job_id).state is JobState.READY
        assert len(store.github_controls("applied")) == 2


def test_cancel_replay_does_not_create_another_job_or_revive_it():
    source = Source(source_issue())
    source.controls = [source_comment("/agentd cancel")]
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        for _ in range(3):
            asyncio.run(intake.poll())
        assert len(store.list_jobs()) == 1
        assert store.get_job(source.issue.job_id).state is JobState.CANCELLED


def test_old_issue_can_be_approved_by_fresh_trusted_comment_only():
    source = Source(
        source_issue(
            created_at="2026-10-01T10:00:00Z", author_login="stranger", author_id=999
        )
    )
    source.controls = [source_comment("/agentd approve")]
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.get_job(source.issue.job_id).state is JobState.READY


def test_approval_comment_does_not_approve_material_issue_changes_after_comment():
    source = Source(
        source_issue(
            created_at="2026-10-01T10:00:00Z",
            material_updated_at="2026-10-03T10:07:00Z",
            author_login="stranger",
            author_id=999,
        )
    )
    source.controls = [source_comment("/agentd approve")]
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.list_jobs() == []
        assert "changed after" in store.github_controls("blocked")[0]["reason"]


def test_review_feedback_calls_only_injected_bounded_handler():
    source = Source(source_issue())
    calls = []

    async def feedback(job_id, instruction, **authority):
        calls.append((job_id, instruction, authority))

    with SQLiteStateStore() as store:
        intake, workflow = compose(
            store,
            source,
            feedback=feedback,
            published_subjects=lambda: {8: source.issue.job_id},
        )
        asyncio.run(intake.poll())
        job = store.get_job(source.issue.job_id)
        for state in (JobState.ADMITTED, JobState.RUNNING, JobState.REVIEW):
            changed, event = transition_job(job, state, "test")
            store.save_job(changed, event, expected=job)
            job = changed
        source.controls = [source_comment("Please improve the tests", number=8)]
        asyncio.run(workflow.poll_controls())
        asyncio.run(workflow.poll_controls())
        assert len(calls) == 1
        assert calls[0][1] == "Please improve the tests"
        assert calls[0][2] == {
            "actor": "github:owner:123",
            "event_id": "github:42:comment:1",
        }


@pytest.mark.parametrize(
    "body",
    ["/agentd pause maximum=999", "/agentd shell rm -rf /", "/agentd approve latest"],
)
def test_comments_cannot_select_configuration(body):
    with pytest.raises(ValueError):
        parse_control(body)


class StatusAdapter:
    def __init__(self):
        self.existing = None
        self.creates = 0
        self.updates = 0
        self.lose_create_response = False
        self.hidden = False

    def verify_repository(self, repository, repository_id):
        assert repository == "owner/repo"
        assert repository_id == 42

    def find_status(self, repository, number, marker):
        return None if self.hidden else self.existing

    def create(self, repository, number, body):
        self.creates += 1
        self.existing = {
            "id": 20,
            "body": body,
            "html_url": "https://github.com/owner/repo/issues/7#issuecomment-20",
        }
        if self.lose_create_response:
            raise TimeoutError("lost successful create response")
        return self.existing

    def update(self, repository, identity, body):
        self.updates += 1
        self.existing["body"] = body
        return self.existing


def test_status_report_deduplication_survives_restart_and_updates_one_comment(tmp_path):
    path = tmp_path / "state.sqlite"
    adapter = StatusAdapter()
    source = Source(source_issue())
    report = {"job_id": source.issue.job_id, "state": "READY", "attempts": 0}
    with SQLiteStateStore(path) as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue([report])
        asyncio.run(reporter.publish_pending())
    with SQLiteStateStore(path) as store:
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue([report])
        assert asyncio.run(reporter.publish_pending()) == ()
        reporter.enqueue(
            [
                {
                    **report,
                    "state": "REVIEW",
                    "publication_stage": "published",
                    "pr": "https://github.com/owner/repo/pull/8",
                }
            ]
        )
        asyncio.run(reporter.publish_pending())
    assert adapter.creates == 1
    assert adapter.updates == 1
    assert "Delivered" in adapter.existing["body"]


def test_lost_status_creation_response_reconciles_without_duplicate_creation(tmp_path):
    path = tmp_path / "state.sqlite"
    adapter = StatusAdapter()
    adapter.lose_create_response = True
    source = Source(source_issue())
    with SQLiteStateStore(path) as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue([{"job_id": source.issue.job_id, "state": "READY"}])
        assert (
            asyncio.run(reporter.publish_pending())[0]["status_error"] == "TimeoutError"
        )
    adapter.hidden = True
    with SQLiteStateStore(path) as store:
        reporter = GitHubStatusReporter(store, adapter)
        assert "status_pending" in asyncio.run(reporter.publish_pending())[0]
        adapter.hidden = False
        assert "status_url" in asyncio.run(reporter.publish_pending())[0]
    assert adapter.creates == 1


def test_graphql_snapshot_must_match_exact_rest_material_and_editor_identity():
    source = GitHubWorkflowSource()
    issue = source_issue()
    node = {
        "id": issue.node_id,
        "title": issue.title,
        "body": issue.body,
        "createdAt": issue.created_at,
        "lastEditedAt": "2026-10-03T10:05:01Z",
        "author": {"login": "owner", "databaseId": 123},
        "editor": {"login": "stranger", "databaseId": 999},
        "timelineItems": {
            "nodes": [
                {
                    "createdAt": "2026-10-03T10:05:02Z",
                    "currentTitle": issue.title,
                    "actor": {"login": "owner", "databaseId": 123},
                }
            ]
        },
    }
    source._graphql = lambda *_args, **_kwargs: {
        "repository": {"databaseId": 42, "issue": node}
    }
    verified, actors = source.issue_authority(issue)
    assert actors == (("owner", 123), ("stranger", 999), ("owner", 123))
    assert verified.material_updated_at == "2026-10-03T10:05:02Z"
    node["body"] = "Raced different intent"
    with pytest.raises(ValueError, match="changed during"):
        source.issue_authority(issue)


def test_existing_policy_requires_immutable_actor_ids_and_timezone():
    with pytest.raises(ValueError, match="actors"):
        StandingGitHubPolicy("owner/repo", 42, {"owner": True}, "2026-10-03T10:00:00Z")
    with pytest.raises(ValueError, match="timezone"):
        StandingGitHubPolicy("owner/repo", 42, {"owner": 123}, "2026-10-03T10:00:00")


def test_active_feedback_survives_restart_and_waits_for_terminal_attempt(tmp_path):
    path = tmp_path / "state.sqlite"
    source = Source(source_issue())
    calls = []

    async def feedback(*args, **kwargs):
        calls.append((args, kwargs))

    with SQLiteStateStore(path) as store:
        intake, workflow = compose(store, source, feedback=feedback)
        asyncio.run(intake.poll())
        source.controls = [source_comment("Improve the diagnostics")]
        store.find_active_run = lambda _job_id: object()
        asyncio.run(workflow.poll_controls())
        assert calls == []
        assert len(store.github_controls("pending")) == 1
    with SQLiteStateStore(path) as store:
        intake, workflow = compose(store, source, feedback=feedback)
        job = store.get_job(source.issue.job_id)
        for state in (JobState.ADMITTED, JobState.RUNNING, JobState.REVIEW):
            changed, event = transition_job(job, state, "test")
            store.save_job(changed, event, expected=job)
            job = changed
        store.list_runs = lambda _job_id: [object()]
        asyncio.run(workflow.apply_pending())
        assert len(calls) == 1
        assert len(store.github_controls("applied")) == 1


def test_pending_control_is_revoked_if_actor_is_removed_from_policy():
    source = Source(source_issue())
    with SQLiteStateStore() as store:
        intake, workflow = compose(store, source)
        asyncio.run(intake.poll())
        comment = source_comment("/agentd cancel")
        from dataclasses import asdict

        store.record_github_control(comment.event_id(42), 7, asdict(comment))
        workflow.policy = StandingGitHubPolicy(
            "owner/repo", 42, {"replacement": 789}, "2026-10-03T10:00:00Z"
        )
        asyncio.run(workflow.apply_pending())
        assert store.get_job(source.issue.job_id).state is JobState.READY
        assert "no longer trusted" in store.github_controls("blocked")[0]["reason"]


def test_status_rejected_before_acceptance_retries_after_credentials_recover():
    adapter = StatusAdapter()
    original = adapter.create

    def rejected(*_args):
        raise GitHubStatusRejected("HTTP 403")

    source = Source(source_issue())
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue([{"job_id": source.issue.job_id, "state": "READY"}])
        adapter.create = rejected
        asyncio.run(reporter.publish_pending())
        assert not store.pending_github_status()[0]["started"]
        adapter.create = original
        assert "status_url" in asyncio.run(reporter.publish_pending())[0]
    assert adapter.creates == 1


def test_status_does_not_write_into_a_recreated_repository():
    adapter = StatusAdapter()

    def changed_repository(*_args):
        raise ValueError("repository identity changed")

    adapter.verify_repository = changed_repository
    source = Source(source_issue())
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue([{"job_id": source.issue.job_id, "state": "READY"}])
        assert (
            asyncio.run(reporter.publish_pending())[0]["status_error"] == "ValueError"
        )
    assert adapter.creates == 0


def test_pr_approval_uses_exact_head_injected_callback_not_issue_approval():
    source = Source(source_issue())
    approvals = []

    async def approve_pr(job_id, **grant):
        approvals.append((job_id, grant))

    with SQLiteStateStore() as store:
        intake, workflow = compose(
            store,
            source,
            published_subjects=lambda: {8: source.issue.job_id},
            approve_pr=approve_pr,
        )
        asyncio.run(intake.poll())
        source.controls = [source_comment("/agentd approve " + "a" * 40, number=8)]
        asyncio.run(workflow.poll_controls())
        assert approvals == [
            (
                source.issue.job_id,
                {
                    "actor": "github:owner:123",
                    "event_id": "github:42:comment:1",
                    "occurred_at": "2026-10-03T10:06:00Z",
                    "head_commit": "a" * 40,
                },
            )
        ]


@pytest.mark.parametrize(
    "body",
    [
        "<!-- agentd-status:abc -->\n\nagentd: READY",
        "<!-- agentd:selfhost-release-status:v1 -->\nAgentd release supervisor",
        "<!-- agentd:host-abandon:abc -->\nAttempt physically retired",
    ],
)
def test_same_account_machine_status_is_ignored_as_feedback(body):
    source = Source(source_issue())
    source.controls = [source_comment(body)]
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert (
            store.get_job(source.issue.job_id).objective
            == compile_job(source.issue).objective
        )
        assert len(store.github_controls("ignored")) == 1
        assert store.github_controls("pending") == []


@pytest.mark.parametrize("body", ["/agentd abandon", "/agentd abandon run-1"])
def test_host_retirement_control_does_not_submit_operations_issue_or_feedback(body):
    source = Source(source_issue(created_at="2026-10-03T09:00:00Z"))
    source.controls = [source_comment(body, number=86)]
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        assert store.list_jobs() == []
        controls = store.github_controls("ignored")
        assert len(controls) == 1
        assert controls[0]["reason"] == "handled by the trusted HP supervisor"
        assert parse_control(body)[0] == "abandon"


def test_unchanged_approval_reuses_provenance_and_actor_revocation_fences_it():
    source = Source(source_issue())
    checked = []
    authority = source.issue_authority

    def verify(issue):
        checked.append(issue.revision)
        return authority(issue)

    source.issue_authority = verify
    with SQLiteStateStore() as store:
        intake, workflow = compose(store, source)
        asyncio.run(intake.poll())
        asyncio.run(intake.poll())
        assert checked == [source.issue.revision]
        workflow.policy = StandingGitHubPolicy(
            "owner/repo", 42, {"replacement": 789}, "2026-10-03T10:00:00Z"
        )
        asyncio.run(intake.poll())
        assert store.github_source_for_job(source.issue.job_id)["revoked"]
        assert store.get_job(source.issue.job_id).state is JobState.BACKLOG


def test_reporter_explains_quota_repair_ownership_and_hold_without_false_revocation():
    source = Source(source_issue())
    adapter = StatusAdapter()
    with SQLiteStateStore() as store:
        intake, _ = compose(store, source)
        asyncio.run(intake.poll())
        store.set_github_job_hold(source.issue.job_id, held=True, event_id="pause")
        reporter = GitHubStatusReporter(store, adapter)
        reporter.enqueue(
            [
                {
                    "job_id": source.issue.job_id,
                    "state": "REVIEW",
                    "authorized": False,
                    "quota": {
                        "provider_wait_reason": "quota_provider_pressure",
                        "local_available": 0,
                    },
                    "repair": {
                        "status": "blocked",
                        "reason": "validation repair budget exhausted",
                    },
                    "blocked_reason": "Execution ownership is unresolved",
                }
            ]
        )
        asyncio.run(reporter.publish_pending())
        body = adapter.existing["body"]
        assert "quota_provider_pressure" in body
        assert "allowance is waiting for renewal" in body
        assert "repair budget exhausted" in body
        assert "ownership is unresolved" in body
        assert "Paused by your GitHub control" in body
        assert "Source approval was revoked" not in body
