from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from agentd.intake.integration import (
    CheckEvidence,
    ExternalImplementationEvidence,
    GitHubIntegrationSource,
    IntegrationEvidenceEvaluator,
    IntegrationEvidenceStore,
    IntegrationPolicy,
    IntegrationStatus,
    PullRequestEvidence,
)


def _pr(**changes):
    values = dict(
        repository="acme/project",
        number=7,
        state="open",
        draft=False,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_branch="main",
        checks=(CheckEvidence("ci", "completed", "success"),),
        merged=True,
        target_commits=("prerequisite",),
        merged_sha="c" * 40,
        target_contains_merge=True,
        observed_at=datetime.now(UTC).isoformat(),
    )
    values.update(changes)
    return PullRequestEvidence(**values)


def test_merged_pr_with_exact_prerequisite_is_ready():
    decision = IntegrationEvidenceEvaluator(
        IntegrationPolicy("acme/project", "main", ("prerequisite",))
    ).evaluate([_pr()])
    assert decision.status is IntegrationStatus.READY


def test_ambiguous_and_untrusted_pr_states_fail_closed():
    evaluator = IntegrationEvidenceEvaluator(
        IntegrationPolicy(
            "acme/project", "main", ("prerequisite",), expected_head_sha="a" * 40
        )
    )
    assert evaluator.evaluate([]).status is IntegrationStatus.BLOCKED
    assert (
        evaluator.evaluate([_pr(), _pr(number=8)]).status is IntegrationStatus.BLOCKED
    )
    assert "head changed" in evaluator.evaluate([_pr(head_sha="c" * 40)]).reason
    assert (
        "failing checks"
        in evaluator.evaluate(
            [_pr(checks=(CheckEvidence("ci", "completed", "failure"),))]
        ).reason
    )
    assert (
        "closed without merge"
        in evaluator.evaluate([_pr(state="closed", merged=False)]).reason
    )


def test_external_requires_explicit_matching_policy():
    policy = IntegrationPolicy(
        "acme/project",
        "main",
        ("p",),
        allow_external=True,
        external_policy_id="admin",
        external_policy_digest="digest",
    )
    evidence = ExternalImplementationEvidence(
        "impl-1", "acme/project", "a" * 40, "admin", "digest", ("p",), True, True
    )
    assert IntegrationEvidenceEvaluator(policy).evaluate(external=evidence).ready


def test_observations_are_append_only():
    db = sqlite3.connect(":memory:")
    store = IntegrationEvidenceStore(db)
    evaluator = IntegrationEvidenceEvaluator(
        IntegrationPolicy("acme/project", "main"), store
    )
    evaluator.evaluate([_pr(target_commits=())])
    assert len(store.list()) == 1


def test_collector_rejects_moved_target_and_untrusted_mentions():
    import pytest

    base = "b" * 40
    calls = {}

    def get(endpoint):
        calls[endpoint] = calls.get(endpoint, 0) + 1
        if "/timeline?" in endpoint:
            return [
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 7,
                            "repository_url": "https://api.github.com/repos/acme/project",
                            "pull_request": {"url": "x"},
                        }
                    },
                }
            ]
        if endpoint.endswith("/branches/main"):
            return {"commit": {"sha": "c" * 40}}
        raise AssertionError(endpoint)

    with pytest.raises(ValueError, match="moved"):
        GitHubIntegrationSource(get=get).observe(
            {"repository": "acme/project", "number": 3, "node_id": "I3"}, base, "main"
        )


def test_collector_explicit_mapping_uses_pinned_target():
    base = "b" * 40
    merge = "c" * 40

    def get(endpoint):
        if "/timeline?" in endpoint:
            return []
        if endpoint.endswith("/branches/main"):
            return {"commit": {"sha": base}}
        if "/pulls/7" in endpoint:
            return {
                "state": "closed",
                "draft": False,
                "merged_at": "now",
                "merge_commit_sha": merge,
                "head": {"sha": "a" * 40},
                "base": {"sha": base, "ref": "main"},
            }
        if "/check-runs?" in endpoint:
            return {"check_runs": []}
        if "/status?" in endpoint:
            return {"statuses": []}
        if "/compare/" in endpoint:
            return {"status": "identical", "total_commits": 0, "commits": []}
        raise AssertionError(endpoint)

    policy = IntegrationPolicy("acme/project", "main", max_age_seconds=900)
    decision = GitHubIntegrationSource(
        get=get, evaluator=IntegrationEvidenceEvaluator(policy)
    ).observe(
        {"repository": "acme/project", "number": 3, "node_id": "I3"},
        base,
        "main",
        linked_pr_numbers=(7,),
    )
    assert decision.ready
    assert decision.links == ("https://github.com/acme/project/pull/7",)


def test_collector_requires_graphql_closing_reference_for_timeline_pr():
    base, merge = "b" * 40, "c" * 40

    def get(endpoint):
        if "/timeline?" in endpoint:
            return [
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 7,
                            "repository_url": "https://api.github.com/repos/acme/project",
                            "pull_request": {"url": "x"},
                        }
                    },
                }
            ]
        if endpoint.endswith("/branches/main"):
            return {"commit": {"sha": base}}
        if "/pulls/7" in endpoint:
            return {
                "state": "closed",
                "draft": False,
                "merged_at": "now",
                "merge_commit_sha": merge,
                "head": {"sha": "a" * 40},
                "base": {"sha": base, "ref": "main"},
            }
        if "/check-runs?" in endpoint:
            return {"check_runs": []}
        if "/status?" in endpoint:
            return {"statuses": []}
        if "/compare/" in endpoint:
            return {"status": "identical", "total_commits": 0, "commits": []}
        raise AssertionError(endpoint)

    def graphql(query, variables):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "closingIssuesReferences": {
                            "nodes": [
                                {
                                    "id": "I3",
                                    "repository": {"nameWithOwner": "acme/project"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    policy = IntegrationPolicy("acme/project", "main")
    decision = GitHubIntegrationSource(
        get=get, graphql=graphql, evaluator=IntegrationEvidenceEvaluator(policy)
    ).observe(
        {"repository": "acme/project", "number": 3, "node_id": "I3"}, base, "main"
    )
    assert decision.ready
    assert decision.links == ("https://github.com/acme/project/pull/7",)


def test_legacy_status_pagination_fails_closed_at_bound():
    source = GitHubIntegrationSource(
        get=lambda endpoint: {
            "statuses": [{"context": str(i), "state": "success"} for i in range(100)]
        }
    )
    import pytest

    with pytest.raises(ValueError, match="commit-status pagination"):
        source._checks("acme/project", "a" * 40)


def test_legacy_status_failure_on_later_page_is_not_hidden():
    def get(endpoint):
        if "/check-runs?" in endpoint:
            return {"check_runs": []}
        if endpoint.endswith("page=1"):
            return {
                "statuses": [
                    {"context": str(i), "state": "success"} for i in range(100)
                ]
            }
        return {"statuses": [{"context": "late-ci", "state": "failure"}]}

    checks = GitHubIntegrationSource(get=get)._checks("acme/project", "a" * 40)
    decision = IntegrationEvidenceEvaluator(
        IntegrationPolicy("acme/project", "main", ("prerequisite",))
    ).evaluate([_pr(checks=checks)])
    assert decision.status is IntegrationStatus.BLOCKED
    assert "failing checks" in decision.reason
