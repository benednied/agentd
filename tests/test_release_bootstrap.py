"""Bootstrap authority must remain bound to the reviewed pull request head."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from watch_selfhost_release import (
    ReleaseBlocked,
    ReleaseWatcher,
    bootstrap_approval,
    bootstrap_checks,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
ACTIVATED_AT = "2026-10-03T12:00:00Z"
ACTORS = frozenset({"maintainer", "other-maintainer"})
ACTOR_IDS = {"maintainer": 7, "other-maintainer": 8}


def pull(**changes):
    return {
        "number": 73,
        "node_id": "PR_bootstrap",
        "state": "open",
        "merged": False,
        "draft": True,
        "body": "",
        "base": {
            "ref": "master",
            "repo": {"full_name": "benednied/agentd", "id": 1328873039},
        },
        "head": {"sha": HEAD},
        **changes,
    }


def review(**changes):
    return {
        "id": 1,
        "user": {"login": "maintainer", "id": 7},
        "state": "APPROVED",
        "commit_id": HEAD,
        "submitted_at": "2026-10-03T12:01:00Z",
        **changes,
    }


def comment(**changes):
    return {
        "id": 10,
        "user": {"login": "maintainer", "id": 7},
        "body": "/agentd approve",
        "created_at": "2026-10-03T12:01:00Z",
        "updated_at": "2026-10-03T12:01:00Z",
        **changes,
    }


def approved(*, pr=None, reviews=(), comments=(), actor_ids=ACTOR_IDS):
    return bootstrap_approval(
        pr or pull(),
        list(reviews),
        list(comments),
        sha=HEAD,
        activated_at=ACTIVATED_AT,
        actors=ACTORS,
        actor_ids=actor_ids,
    )


def check(**changes):
    return {
        "id": 100,
        "name": "quality",
        "app": {"slug": "github-actions"},
        "head_sha": HEAD,
        "started_at": "2026-10-03T12:01:00Z",
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def checked(*runs, required=("quality",)):
    return bootstrap_checks(list(runs), list(required), HEAD)


def test_current_head_native_review_grants_bootstrap_authority():
    assert approved(reviews=[review()])
    assert not approved(reviews=[review(commit_id=OTHER_HEAD)])
    assert not approved(pr=pull(head={"sha": OTHER_HEAD}), reviews=[review()])


@pytest.mark.parametrize(
    "submitted_at",
    ["2026-10-03T11:59:59Z", ACTIVATED_AT, "invalid", None],
)
def test_native_approval_requires_provable_time_after_activation(submitted_at):
    assert not approved(reviews=[review(submitted_at=submitted_at)])


@pytest.mark.parametrize(
    "actor",
    [
        {"login": "maintainer", "id": 9},
        {"login": "maintainer"},
        {"login": "outsider", "id": 7},
        {"login": "outsider", "id": 99},
    ],
)
def test_both_native_reviews_and_issue_comments_require_bound_actor_identity(actor):
    assert not approved(reviews=[review(user=actor)])
    assert not approved(comments=[comment(user=actor)])


def test_bootstrap_never_accepts_login_only_configuration():
    assert not approved(reviews=[review()], actor_ids=None)
    assert not approved(comments=[comment()], actor_ids={})


def test_latest_meaningful_review_wins_even_when_api_order_is_reversed():
    revoked = review(
        id=2,
        state="DISMISSED",
        submitted_at="2026-10-03T12:02:00Z",
    )
    assert not approved(reviews=[revoked, review()])
    restored = review(id=3, submitted_at="2026-10-03T12:03:00Z")
    assert approved(reviews=[restored, revoked, review()])
    discussion = review(id=4, state="COMMENTED", commit_id=OTHER_HEAD)
    assert approved(reviews=[review(), discussion])


def test_review_id_breaks_same_timestamp_ties():
    assert not approved(reviews=[review(id=2, state="DISMISSED"), review(id=1)])
    assert approved(reviews=[review(id=2), review(id=1, state="DISMISSED")])


def test_any_latest_trusted_changes_requested_vetoes_reviews_and_comments():
    blocker = review(
        id=2,
        user={"login": "other-maintainer", "id": 8},
        state="CHANGES_REQUESTED",
        commit_id=OTHER_HEAD,
        submitted_at="2026-10-03T11:59:59Z",
    )
    assert not approved(reviews=[review(), blocker], comments=[comment()])
    cleared = {**blocker, "id": 3, "state": "DISMISSED"}
    assert approved(reviews=[blocker, cleared, review()])
    untrusted = {**blocker, "user": {"login": "other-maintainer", "id": 99}}
    assert approved(reviews=[review(), untrusted])


@pytest.mark.parametrize("body", ["/agentd approve", f"/agentd approve {HEAD}"])
def test_explicit_post_activation_issue_comment_can_approve_exact_head(body):
    assert approved(comments=[comment(body=body)])


@pytest.mark.parametrize(
    "body",
    [
        f"/agentd approve {OTHER_HEAD}",
        f"/agentd approve {HEAD.upper()}",
        "/agentd approve this PR",
        "Please /agentd approve",
        "> /agentd approve",
        "```\n/agentd approve\n```",
        "/agentd approve\nAdditional approval instructions",
        "/agentd approve <!-- agentd:status -->",
    ],
)
def test_issue_comment_approval_rejects_ambiguous_body_or_other_revision(body):
    assert not approved(comments=[comment(body=body)])


@pytest.mark.parametrize(
    "created_at",
    ["2026-10-03T11:59:59Z", ACTIVATED_AT, "invalid", None],
)
def test_issue_comment_requires_provable_time_after_activation(created_at):
    assert not approved(comments=[comment(created_at=created_at)])


def test_pull_request_description_and_comment_edit_do_not_grant_authority():
    assert not approved(pr=pull(body="/agentd approve"))
    assert not approved(
        comments=[
            comment(
                created_at="2026-10-03T11:59:59Z",
                updated_at="2026-10-03T12:05:00Z",
            )
        ]
    )


def test_issue_command_edited_after_activation_cannot_grant_authority():
    assert not approved(
        comments=[
            comment(
                created_at="2026-10-03T12:01:00Z",
                updated_at="2026-10-03T12:02:00Z",
            )
        ]
    )


@pytest.mark.parametrize("updated_at", ["invalid", None])
def test_issue_command_requires_valid_update_timestamp(updated_at):
    assert not approved(comments=[comment(updated_at=updated_at)])


def test_issue_command_requires_update_timestamp_to_prove_no_edit():
    incomplete = comment()
    del incomplete["updated_at"]
    assert not approved(comments=[incomplete])


def test_all_required_checks_must_succeed_on_exact_head():
    assert checked(check())
    assert not checked()
    assert not checked(check(), required=("quality", "security"))
    assert checked(
        check(), check(id=101, name="security"), required=("quality", "security")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": OTHER_HEAD},
        {"app": {"slug": "untrusted-app"}},
        {"app": None},
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": "success"},
        {"conclusion": "failure"},
        {"conclusion": "neutral"},
        {"conclusion": "skipped"},
    ],
)
def test_required_check_rejects_stale_non_actions_or_unsuccessful_evidence(changes):
    assert not checked(check(**changes))


def test_latest_required_check_failure_supersedes_earlier_success():
    failed = check(
        id=101,
        started_at="2026-10-03T12:02:00Z",
        conclusion="failure",
    )
    assert not checked(failed, check())
    successful_retry = check(id=102, started_at="2026-10-03T12:03:00Z")
    assert checked(successful_retry, check(), failed)
    assert not checked(check(), {**successful_retry, "head_sha": OTHER_HEAD})


def test_latest_check_id_breaks_same_timestamp_ties():
    assert not checked(check(id=101, conclusion="failure"), check(id=100))
    assert checked(check(id=101), check(id=100, conclusion="failure"))


def test_newer_queued_check_with_no_start_time_vetoes_old_success():
    assert not checked(
        check(),
        check(id=101, status="queued", conclusion=None, started_at=None),
    )


def test_unrelated_checks_do_not_override_required_actions_evidence():
    assert checked(check(), check(id=101, name="optional", conclusion="failure"))
    assert not checked(
        check(conclusion="failure"),
        check(id=101, app={"slug": "untrusted-app"}),
    )


@pytest.fixture
def bootstrap_rig(tmp_path):
    watcher = ReleaseWatcher(
        {
            "source_repository": str(tmp_path / "source"),
            "release_root": str(tmp_path / "releases"),
            "status_file": str(tmp_path / "state/release-status.json"),
            "gh_config_dir": str(tmp_path / "github-read"),
            "approved_actors": sorted(ACTORS),
            "approved_actor_ids": ACTOR_IDS,
            "bootstrap_pr": {
                "number": 73,
                "head_commit": HEAD,
                "activated_at": ACTIVATED_AT,
                "required_checks": ["quality"],
            },
        }
    )
    state = {
        "pr": pull(),
        "reviews": [review()],
        "comments": [],
        "checks": [check()],
        "lose_ready": False,
        "lose_merge": False,
        "apply_merge": True,
        "after_ready": lambda: None,
    }
    events = []
    writes = []

    def github(endpoint, *, paginated=False):
        if endpoint == "repos/benednied/agentd/pulls/73":
            events.append("read_pull")
            return copy.deepcopy(state["pr"])
        if endpoint == "repos/benednied/agentd/pulls/73/reviews":
            assert paginated
            events.append("read_reviews")
            return copy.deepcopy(state["reviews"])
        if endpoint == "repos/benednied/agentd/issues/73/comments":
            assert paginated
            events.append("read_comments")
            return copy.deepcopy(state["comments"])
        pytest.fail(f"Unexpected GitHub read: {endpoint}")

    def status_api(endpoint, *, data=None, paginated=False, method="GET"):
        writes.append((endpoint, method, data))
        if endpoint == "graphql":
            events.append("ready")
            assert method == "POST"
            assert "markPullRequestReadyForReview" in data["query"]
            assert data["variables"] == {"id": "PR_bootstrap"}
            state["pr"]["draft"] = False
            state["after_ready"]()
            if state["lose_ready"]:
                raise ReleaseBlocked("lost_ready_response")
            return {
                "data": {
                    "markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}
                }
            }
        if endpoint == "repos/benednied/agentd/pulls/73/merge":
            events.append("merge")
            assert method == "PUT"
            assert data == {"sha": HEAD, "merge_method": "merge"}
            if state["apply_merge"]:
                state["pr"].update(
                    merged_at="2026-10-03T12:05:00Z",
                    merge_commit_sha=OTHER_HEAD,
                    state="closed",
                )
            if state["lose_merge"]:
                raise ReleaseBlocked("lost_merge_response")
            return {"merged": state["apply_merge"], "sha": OTHER_HEAD}
        pytest.fail(f"Unexpected GitHub write: {endpoint}")

    def evidence(sha):
        assert sha == HEAD
        events.append("evidence")

    def checks(sha):
        assert sha == HEAD
        events.append("checks")
        return copy.deepcopy(state["checks"])

    watcher.github = github
    watcher.status_api = status_api
    watcher.require_bootstrap_evidence = evidence
    watcher.bootstrap_check_runs = checks
    return watcher, state, events, writes


@pytest.mark.parametrize("pending_gate", ["approval", "checks"])
def test_bootstrap_waits_without_writes_or_master_activation(
    bootstrap_rig, pending_gate
):
    watcher, state, _events, writes = bootstrap_rig
    state["reviews" if pending_gate == "approval" else "checks"] = []
    watcher.inspect_health = lambda: None
    watcher.git = lambda *_args: pytest.fail(
        "Pending bootstrap must block master activation"
    )
    watcher.tick()
    assert writes == []
    assert watcher.status["stage"] == f"bootstrap_waiting_{pending_gate}"


def test_bootstrap_package_evidence_is_required_before_any_github_write(bootstrap_rig):
    watcher, _state, _events, writes = bootstrap_rig

    def unqualified(_sha):
        raise ReleaseBlocked("bootstrap_package_unqualified")

    watcher.require_bootstrap_evidence = unqualified
    with pytest.raises(ReleaseBlocked, match="bootstrap_package_unqualified"):
        watcher.reconcile_bootstrap()
    assert writes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"number": 74},
        {"head": {"sha": OTHER_HEAD}},
        {
            "base": {
                "ref": "other",
                "repo": {"full_name": "benednied/agentd", "id": 1328873039},
            }
        },
        {
            "base": {
                "ref": "master",
                "repo": {"full_name": "outsider/agentd", "id": 1328873039},
            }
        },
        {"base": {"ref": "master", "repo": {"full_name": "benednied/agentd", "id": 1}}},
        {"state": "closed"},
    ],
)
def test_bootstrap_identity_or_closed_unmerged_pr_blocks_writes(bootstrap_rig, changes):
    watcher, state, _events, writes = bootstrap_rig
    state["pr"].update(changes)
    with pytest.raises(ReleaseBlocked):
        watcher.reconcile_bootstrap()
    assert writes == []


def test_bootstrap_draft_ready_and_merge_require_repeated_fresh_gate_proofs(
    bootstrap_rig,
):
    watcher, _state, events, writes = bootstrap_rig
    assert watcher.reconcile_bootstrap() is False
    ready_index = events.index("ready")
    merge_index = events.index("merge")
    assert events[:ready_index].count("evidence") == 1
    assert events[:ready_index].count("checks") == 1
    assert events[ready_index + 1 : merge_index].count("evidence") >= 2
    assert events[ready_index + 1 : merge_index].count("checks") >= 2
    assert events[-1] == "read_pull"
    assert len(writes) == 2
    assert watcher.status["stage"] == "bootstrap_merged"
    assert watcher.status["bootstrap_merge_commit"] == OTHER_HEAD


def test_bootstrap_approval_revoked_after_ready_prevents_merge(bootstrap_rig):
    watcher, state, _events, writes = bootstrap_rig
    state["after_ready"] = lambda: state.update(
        reviews=[review(id=2, state="CHANGES_REQUESTED")]
    )
    assert watcher.reconcile_bootstrap() is True
    assert [endpoint for endpoint, _method, _data in writes] == ["graphql"]
    assert watcher.status["stage"] == "bootstrap_waiting_approval"


def test_bootstrap_head_changed_after_ready_prevents_merge(bootstrap_rig):
    watcher, state, _events, writes = bootstrap_rig
    state["after_ready"] = lambda: state["pr"].update(head={"sha": OTHER_HEAD})
    with pytest.raises(ReleaseBlocked, match="bootstrap_pull_identity_or_head_changed"):
        watcher.reconcile_bootstrap()
    assert [endpoint for endpoint, _method, _data in writes] == ["graphql"]


def test_bootstrap_lost_ready_and_merge_responses_are_reconciled_from_get(
    bootstrap_rig,
):
    watcher, state, _events, writes = bootstrap_rig
    state.update(lose_ready=True, lose_merge=True)
    assert watcher.reconcile_bootstrap() is False
    assert watcher.status["bootstrap_merge_commit"] == OTHER_HEAD
    assert len(writes) == 2
    assert watcher.reconcile_bootstrap() is False
    assert len(writes) == 2


def test_bootstrap_unconfirmed_merge_response_cannot_record_success(bootstrap_rig):
    watcher, state, _events, _writes = bootstrap_rig
    state.update(apply_merge=False, lose_merge=True)
    with pytest.raises(ReleaseBlocked, match="bootstrap_merge_response_unconfirmed"):
        watcher.reconcile_bootstrap()
    assert "bootstrap_merge_commit" not in watcher.status


def test_existing_non_draft_bootstrap_requires_proofs_and_exact_sha_merge(
    bootstrap_rig,
):
    watcher, state, events, writes = bootstrap_rig
    state["pr"]["draft"] = False
    assert watcher.reconcile_bootstrap() is False
    assert "ready" not in events
    assert [endpoint for endpoint, _method, _data in writes] == [
        "repos/benednied/agentd/pulls/73/merge"
    ]
