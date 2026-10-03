"""Bounded trusted validation repair across real Git and worker transport."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_coding_recovery import coding_rig
from test_github_coding_pipeline import ControlledProvider, ControlledValidation

from agentd.coding.pipeline import CodingPublicationReconciler, CodingRepairReconciler
from agentd.coordinator import LifecycleError
from agentd.domain.enums import JobState, RunOutcome
from agentd.domain.models import RunResult, TokenUsage
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.publication import DraftPublisher, PublicationStore, TrustedFinalizer
from agentd.workers import OperationJournal, RemoteWorkerBackend, WorkerServer
from agentd.workers.coding import CodingHarnessDriver


class CompletingProvider(ControlledProvider):
    def __init__(self, values=("broken", "verified"), outcome=RunOutcome.COMPLETED):
        self.starts = 0
        self.values = values
        self.outcome = outcome
        self.executions = []

    async def start_managed(self, run_id, execution):
        self.executions.append(execution)
        return await super().start_managed(run_id, execution)

    async def collect(self, run):
        path = Path(self.execution.working_directory)
        (path / "change.txt").write_text(
            self.values[min(self.starts - 1, len(self.values) - 1)]
        )
        return RunResult(
            self.outcome,
            "untrusted model completion claim",
            usage=TokenUsage(input_tokens=12),
            metadata={"telemetry_valid": True},
        )


class GitHubFixture:
    def __init__(self):
        self.head = self.pr = None
        self.pushes = self.creates = self.updates = self.edits = 0

    def branch_commit(self, intent):
        return self.head

    def push(self, intent, repository):
        self.pushes += 1
        self.head = intent.result_commit

    def find_pr(self, intent):
        return self.pr

    def create_draft(self, intent, body):
        self.creates += 1
        self.pr = {
            "headRefName": intent.branch,
            "headRefOid": intent.result_commit,
            "baseRefName": intent.base_branch,
            "isDraft": True,
            "isCrossRepository": False,
            "state": "OPEN",
            "body": body,
            "url": "https://github.com/test/repo/pull/1",
        }
        return self.pr

    def update_branch(self, intent, repository, expected_commit):
        assert self.head == expected_commit
        self.updates += 1
        self.head = self.pr["headRefOid"] = intent.result_commit

    def update_pr(self, intent, pr, body):
        self.edits += 1
        self.pr["body"] = body
        return self.pr


def pipeline(rig, *, maximum_attempts=3):
    github = GitHubFixture()
    publications = CodingPublicationReconciler(
        rig.store,
        DraftPublisher(
            PublicationStore(rig.store.path),
            github,
            TrustedFinalizer(ControlledValidation()),
        ),
        {rig.profile.id: rig.profile},
        {rig.profile.repository: rig.path / "repo"},
        {rig.profile.repository: "master"},
    )
    repairs = CodingRepairReconciler(
        rig.store, publications, rig.coordinator, maximum_attempts=maximum_attempts
    )
    return github, publications, repairs


async def complete_attempt(rig, expected_state=JobState.REVIEW):
    await rig.plane.refresh_worker_heartbeats()
    run = await rig.plane.dispatch_next()
    assert run is not None
    for _ in range(100):
        await rig.coordinator.reconcile_managed_runs()
        if rig.store.get_job(rig.job.id).state is expected_state:
            return rig.store.get_run(run.id)
        await asyncio.sleep(0.01)
    raise AssertionError("coding attempt did not finish")


VALIDATION = (
    (
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('change.txt').read_text() == 'verified'",
    ),
)


def test_validation_failure_repairs_new_candidate_under_one_cumulative_budget(tmp_path):
    async def scenario():
        provider = CompletingProvider()
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=VALIDATION
        ) as rig:
            rig.quota()
            original = await complete_attempt(rig)
            github, publications, repairs = pipeline(rig)
            await publications.reconcile()
            assert (
                publications.publisher.store.get(rig.job.id)["stage"]
                == "validation_failed"
            )
            assert github.creates == github.pushes == 0
            assert (await repairs.reconcile())[0]["repair_status"] == "queued"
            assert rig.store.get_run(original.id) == original
            assert provider.starts == 1  # capture/queue cannot start a provider
            repaired = await complete_attempt(rig)
            assert repaired.id != original.id
            assert (
                repaired.contract.operation.work_order.resume_from_run_id == original.id
            )
            assert repaired.contract.operation.work_order.prior_consumed_quota == 12
            assert "AssertionError" in provider.executions[-1].completion_protocol
            assert rig.store.get_job(rig.job.id).quota_budget.maximum == 200
            assert (
                sum(item.consumed for item in rig.store.list_reservations(rig.job.id))
                == 24
            )
            assert rig.store.get_quota_pool("account").remaining == 976
            await publications.reconcile()
            assert github.creates == github.pushes == 1
            assert provider.starts == 2
            assert len(publications.publisher.store.candidates(rig.job.id)) == 2
            assert (
                publications.publisher.store.get(rig.job.id)["delivery"]["run_id"]
                == repaired.id
            )
            assert await repairs.reconcile() == ()
            assert await rig.plane.dispatch_next() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["held", "revoked", "attempts", "maximum"])
def test_unattended_repair_stops_on_authority_and_resource_limits(tmp_path, blocked):
    async def scenario():
        provider = CompletingProvider()
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=VALIDATION
        ) as rig:
            rig.quota()
            original = await complete_attempt(rig)
            github, publications, repairs = pipeline(
                rig, maximum_attempts=1 if blocked == "attempts" else 3
            )
            await publications.reconcile()
            if blocked == "held":
                rig.store.set_github_job_hold(
                    rig.job.id, held=True, event_id="pause-event"
                )
            elif blocked == "revoked":
                source = rig.store.github_source_for_job(rig.job.id)
                import json

                issue = SourceIssue.from_dict(json.loads(source["payload"]))
                rig.store.observe_github_issue(
                    replace(issue, state="closed"), IntakePolicy("test/repo", 7)
                )
            elif blocked == "maximum":
                job = rig.store.get_job(rig.job.id)
                rig.store.save_job(
                    replace(
                        job,
                        quota_budget=replace(
                            job.quota_budget, implementation=12, maximum=12
                        ),
                    ),
                    expected=job,
                )
            result = (await repairs.reconcile())[0]
            assert result["repair"]["status"] in {"blocked", "exhausted"}
            assert rig.store.get_job(rig.job.id).state is JobState.REVIEW
            assert rig.store.get_run(original.id) == original
            assert provider.starts == 1
            assert github.creates == github.pushes == 0

    asyncio.run(scenario())


def test_trusted_github_feedback_updates_delivered_pr_in_fresh_attempt(tmp_path):
    async def scenario():
        provider = CompletingProvider(values=("first", "second"))
        commands = (
            (
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "assert Path('change.txt').read_text() in {'first', 'second'}",
            ),
        )
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=commands
        ) as rig:
            rig.quota()
            first = await complete_attempt(rig)
            github, publications, repairs = pipeline(rig)
            await publications.reconcile()
            original_pr = github.pr.copy()
            await repairs.request_feedback(
                rig.job.id,
                "Please improve the implementation",
                actor="maintainer",
                event_id="comment:123",
            )
            assert (
                await repairs.request_feedback(
                    rig.job.id,
                    "Please improve the implementation",
                    actor="maintainer",
                    event_id="comment:123",
                )
            )["repair_status"] == "queued"
            assert rig.store.get_run(first.id) == first
            second = await complete_attempt(rig)
            assert (
                "Please improve the implementation"
                in provider.executions[-1].completion_protocol
            )
            await publications.reconcile()
            assert github.pr["url"] == original_pr["url"]
            assert github.pr["headRefOid"] == second.result.commit
            assert github.pushes == github.creates == github.updates == 1
            assert github.edits == 1
            assert provider.starts == 2
            await repairs.request_feedback(
                rig.job.id,
                "Please improve the implementation",
                actor="maintainer",
                event_id="comment:123",
            )
            assert rig.store.get_job(rig.job.id).state is JobState.REVIEW
            assert await rig.plane.dispatch_next() is None

    asyncio.run(scenario())


def test_github_retry_restores_proven_failed_partial_run_without_erasing_history(
    tmp_path,
):
    async def scenario():
        provider = CompletingProvider(
            values=("partial", "verified"), outcome=RunOutcome.FAILED
        )
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=VALIDATION
        ) as rig:
            rig.quota()
            first = await complete_attempt(rig, JobState.FAILED)
            _, publications, repairs = pipeline(rig)
            with pytest.raises(LifecycleError, match="stopped metered"):
                await rig.coordinator.queue_coding_repair(
                    rig.job.id, diagnostics=("automatic",)
                )
            await repairs.request_feedback(
                rig.job.id,
                "Try again from the retained edits",
                actor="maintainer",
                event_id="comment:retry",
            )
            assert rig.store.get_run(first.id) == first
            provider.outcome = RunOutcome.COMPLETED
            second = await complete_attempt(rig)
            assert second.contract.operation.work_order.prior_consumed_quota == 12
            assert second.contract.operation.work_order.resume_from_run_id == first.id
            assert rig.store.get_run(first.id).result.outcome is RunOutcome.FAILED
            await publications.reconcile()
            assert publications.publisher.store.get(rig.job.id)["delivery"]
            assert (
                sum(item.consumed for item in rig.store.list_reservations(rig.job.id))
                == 24
            )

    asyncio.run(scenario())


def test_completed_checkpoint_capture_survives_worker_restart(tmp_path):
    async def scenario():
        provider = CompletingProvider(values=("verified", "verified"))
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=VALIDATION
        ) as rig:
            rig.quota()
            first = await complete_attempt(rig)
            original_result = await rig.backend.collect(first.id)
            await rig.server.close()
            journal = OperationJournal(
                tmp_path / "worker.sqlite", node_id="worker", session_epoch="epoch"
            )
            rig.worker = CodingHarnessDriver(
                tmp_path / "worker",
                {rig.profile.id: rig.profile},
                {"codex": provider},
                runner=rig.worker.runner,
                account_pools={"codex": "account"},
            )
            restarted = WorkerServer(
                "127.0.0.1",
                0,
                node_id="worker",
                session_epoch="epoch",
                secret=b"q" * 32,
                drivers=[rig.worker],
                journal=journal,
                allow_insecure_loopback=True,
            )
            await restarted.start()
            rig.server = restarted
            try:
                await rig.reopen_controller()
                assert isinstance(rig.backend, RemoteWorkerBackend)
                checkpoint = await rig.backend.capture_coding_checkpoint(first.id)
                assert checkpoint["result_commit"] == first.result.commit
                assert checkpoint["cumulative_quota"] == 12
                assert await rig.backend.collect(first.id) == original_result
                status = await rig.backend.status(first.id)
                assert status["result"] == original_result.to_dict()
                assert (
                    journal.load_run_result(run_id=first.id)
                    == original_result.to_dict()
                )
                await rig.coordinator.queue_coding_repair(
                    rig.job.id,
                    diagnostics=("Restarted worker feedback",),
                    actor="maintainer",
                    event_id="comment:restart",
                )
                assert rig.store.get_job(rig.job.id).state is JobState.READY
                assert rig.store.get_run(first.id) == first
                assert provider.starts == 1
            finally:
                await restarted.close()

    asyncio.run(scenario())


def test_github_retry_of_failed_feedback_keeps_original_delivery_and_one_pr(tmp_path):
    async def scenario():
        provider = CompletingProvider(values=("verified", "partial", "verified"))
        async with coding_rig(
            tmp_path, provider=provider, validation_commands=VALIDATION
        ) as rig:
            rig.quota()
            first = await complete_attempt(rig)
            github, publications, repairs = pipeline(rig)
            await publications.reconcile()
            provider.outcome = RunOutcome.FAILED
            await repairs.request_feedback(
                rig.job.id,
                "Improve it",
                actor="maintainer",
                event_id="comment:first",
            )
            failed = await complete_attempt(rig, JobState.FAILED)
            assert (
                publications.publisher.store.get(rig.job.id)["delivery"]["run_id"]
                == first.id
            )
            await repairs.request_feedback(
                rig.job.id,
                "Retry the failed improvement",
                actor="maintainer",
                event_id="comment:retry",
            )
            provider.outcome = RunOutcome.COMPLETED
            repaired = await complete_attempt(rig)
            assert rig.store.get_run(failed.id) == failed
            await publications.reconcile()
            assert github.creates == github.pushes == github.updates == 1
            assert (
                publications.publisher.store.get(rig.job.id)["delivery"]["run_id"]
                == repaired.id
            )
            assert provider.starts == 3
            assert (
                sum(item.consumed for item in rig.store.list_reservations(rig.job.id))
                == 36
            )

    asyncio.run(scenario())
