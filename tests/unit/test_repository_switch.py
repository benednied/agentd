"""Repository switches retain old delivery without reusing its authority."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler, CodingRepairReconciler
from agentd.coordinator import LifecycleError
from agentd.domain.enums import JobState, QuotaUnit
from agentd.domain.models import EffortEstimate, QuotaBudget
from agentd.domain.transitions import transition_job
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.publication import PublicationStore
from agentd.state.sqlite import SQLiteStateStore


@pytest.mark.parametrize("state", [JobState.REVIEW, JobState.FAILED])
def test_foreign_repository_never_publishes_or_requests_a_repair(tmp_path, state):
    with SQLiteStateStore(tmp_path / "state.sqlite") as store:
        issue = SourceIssue(
            "owner/old", 41, 7, "I_old", "Fix", "Intent", "2026-10-03T10:00:00Z"
        )
        profile = RepositoryProfile(
            "old", "v1", issue.repository, "https://github.com/owner/old.git"
        )
        policy = IntakePolicy(issue.repository, issue.repository_id, None)
        store.observe_github_issue(issue, policy)
        store.approve_github_issue(issue, policy, actor="trusted-fixture")
        compiler = CodingJobCompiler(
            profile,
            "a" * 40,
            QuotaBudget(10, maximum=20, unit=QuotaUnit.TOKENS),
            EffortEstimate(1, 2),
        )
        job = store.create_github_job(issue, compiler(issue))
        for next_state in (JobState.ADMITTED, JobState.RUNNING, state):
            updated, event = transition_job(job, next_state, "fixture execution")
            store.save_job(updated, event, expected=job)
            job = updated
        current = replace(
            profile,
            id="current",
            repository="owner/current",
            clone_url="https://github.com/owner/current.git",
        )
        publications = CodingPublicationReconciler(
            store,
            SimpleNamespace(store=PublicationStore(store.path)),
            {current.id: current},
            {current.repository: tmp_path / "objects"},
            {current.repository: "master"},
        )

        def forbidden(_job_id):
            raise AssertionError("inactive repository attempted external publication")

        publications.publish_job = forbidden
        repairs = CodingRepairReconciler(store, publications, object())
        assert asyncio.run(publications.reconcile()) == ()
        assert asyncio.run(repairs.reconcile()) == ()
        with pytest.raises(LifecycleError, match="outside the current policy"):
            asyncio.run(
                repairs.request_feedback(
                    job.id, "Change it", actor="trusted-fixture", event_id="feedback"
                )
            )
        assert store.get_job(job.id) == job
        assert PublicationStore(store.path).get(job.id) is None
