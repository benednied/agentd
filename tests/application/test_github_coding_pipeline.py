"""Real Git and authenticated TCP across intake, scheduling and publication."""

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.descriptor import RemoteCodingDescriptor
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler
from agentd.coordinator import SchedulerCoordinator
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome
from agentd.domain.models import (
    EffortEstimate,
    HarnessCapabilities,
    ProviderQuotaSnapshot,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    RunHandle,
    RunResult,
    WorkerNode,
)
from agentd.harness.registry import DriverRegistry
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.publication import DraftPublisher, PublicationStore, TrustedFinalizer
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers import (
    BackendRegistry,
    OperationJournal,
    RemoteWorkerBackend,
    RemoteWorkerClient,
    WorkerServer,
)
from agentd.workers.coding import CodingHarnessDriver
from agentd.workers.operations import SubprocessCommandRunner
from agentd.workspaces.git import GitWorkspaceManager


def git(path, *args):
    return subprocess.check_output(("git", "-C", str(path), *args)).decode().strip()


class FixtureClone(SubprocessCommandRunner):
    def __init__(self, path):
        super().__init__()
        object.__setattr__(self, "path", path)

    async def run(self, argv, **kwargs):
        return await super().run(
            tuple(
                str(self.path) if arg == "https://github.com/test/repo.git" else arg
                for arg in argv
            ),
            **kwargs,
        )


class ControlledProvider:
    starts = 0

    def capabilities(self):
        return HarnessCapabilities(
            "codex",
            frozenset({"standard"}),
            frozenset(
                {
                    "credential-isolated",
                    "restricted-workspace-write",
                    "network-disabled",
                }
            ),
        )

    async def start_managed(self, run_id, execution):
        self.starts += 1
        self.execution = execution
        return RunHandle(run_id, "codex")

    async def start(self, execution):
        raise AssertionError("unmanaged")

    async def recover(self, run_id, execution, recovery_instruction=""):
        raise AssertionError("duplicate execution")

    def observe(self, run_id):
        return None

    async def collect(self, run):
        path = Path(self.execution.working_directory)
        (path / "change.txt").write_text("verified\n")
        git(path, "add", "change.txt")
        git(
            path,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-m",
            "Change",
        )
        return RunResult(
            RunOutcome.COMPLETED, "false model tests passed", consumed_quota=12
        )

    async def cancel(self, run):
        pass

    async def interrupt(self, run):
        pass

    async def steer(self, run, instruction):
        raise AssertionError("unbounded intent")


class ControlledValidation:
    def run(self, command, *, cwd, env, timeout):
        # Only this test fixture's independently specified process is permitted.
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
            text=True,
            capture_output=True,
        )


class AmbiguousGitHub:
    head = None
    pr = None
    pushes = 0
    creates = 0

    def branch_commit(self, intent):
        return self.head

    def push(self, intent, repository):
        self.pushes += 1
        self.head = intent.result_commit
        raise OSError("push succeeded but acknowledgement lost")

    def find_pr(self, intent):
        return self.pr

    def create_draft(self, intent, body):
        self.creates += 1
        self.pr = {
            "headRefName": intent.branch,
            "headRefOid": intent.result_commit,
            "baseRefName": intent.base_branch,
            "isDraft": True,
            "url": "https://github.com/test/repo/pull/1",
            "body": body,
        }
        raise OSError("PR created but acknowledgement lost")


def test_issue_to_one_remote_run_and_reconciled_draft(tmp_path):
    async def scenario():
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "master")
        (repo / "README").write_text("base\n")
        git(repo, "add", ".")
        git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-m",
            "Base",
        )
        base = git(repo, "rev-parse", "HEAD")
        profile = RepositoryProfile(
            "repo",
            "v1",
            "test/repo",
            "https://github.com/test/repo.git",
            validation_commands=(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('change.txt').read_text() == 'verified\\n'",
                ),
            ),
        )
        compiler = CodingJobCompiler(
            profile,
            base,
            QuotaBudget(100, maximum=200, pool_id="account", unit=QuotaUnit.TOKENS),
            EffortEstimate(1, 2),
        )
        issue = SourceIssue(
            "test/repo",
            7,
            1,
            "I_1",
            "Make change",
            "Intent",
            "2026-09-18T10:00:00Z",
            labels=("agentd:approved",),
        )
        policy = IntakePolicy("test/repo", 7)
        store = SQLiteStateStore(tmp_path / "controller.sqlite")
        provider = ControlledProvider()
        worker = CodingHarnessDriver(
            tmp_path / "worker",
            {"repo": profile},
            {"codex": provider},
            runner=FixtureClone(repo),
            account_pools={"codex": "account"},
        )
        journal = OperationJournal(
            tmp_path / "worker.sqlite", node_id="worker", session_epoch="epoch"
        )
        server = WorkerServer(
            "127.0.0.1",
            0,
            node_id="worker",
            session_epoch="epoch",
            secret=b"q" * 32,
            drivers=[worker],
            journal=journal,
            allow_insecure_loopback=True,
        )
        await server.start()
        host, port = server.address
        client = RemoteWorkerClient(
            host,
            port,
            node_id="worker",
            session_epoch="epoch",
            secret=b"q" * 32,
            allow_insecure_loopback=True,
        )
        backend = RemoteWorkerBackend(
            client, name="remote", node_id="worker", expected_driver="remote-coding"
        )
        descriptor = RemoteCodingDescriptor(worker.capabilities().features)
        coordinator = SchedulerCoordinator(
            store,
            GitWorkspaceManager(tmp_path / "unused-controller-worktrees"),
            DriverRegistry([descriptor]),
            backends=BackendRegistry([backend]),
        )
        plane = ControlPlane(store, coordinator=coordinator)
        plane.register_node(
            WorkerNode(
                "worker",
                capacity=ResourceVector(2, 2),
                harnesses=frozenset({"remote-coding"}),
                labels={"backend": "remote"},
            )
        )
        plane.register_quota_pool(
            QuotaPool("account", "codex", 1000, unit=QuotaUnit.TOKENS)
        )
        store.observe_github_issue(issue, policy)
        store.approve_github_issue(issue, policy, actor="operator")
        job = compiler(issue)
        plane._validate_artifact_intent(job)
        store.create_github_job(issue, job)
        store.create_github_job(issue, job)
        try:
            assert await plane.dispatch_next() is None  # no compatible live worker
            await plane.refresh_worker_heartbeats()
            assert await plane.dispatch_next() is None  # unknown quota
            assert coordinator.quota_wait_reason(job.id) == "quota_unknown"
            store.append_provider_quota_snapshot(
                ProviderQuotaSnapshot(
                    pool_id="account", bucket_id="account", primary_used_percent=10
                )
            )
            run = await plane.dispatch_next()
            assert run is not None
            for _ in range(100):
                await coordinator.reconcile_managed_runs()
                if store.get_job(job.id).state is JobState.REVIEW:
                    break
                await asyncio.sleep(0.01)
            assert store.get_job(job.id).state is JobState.REVIEW
            assert provider.starts == 1
            assert store.get_quota_pool("account").remaining == 988
            assert (
                list((tmp_path / "unused-controller-worktrees").glob("**/.git")) == []
            )
            github = AmbiguousGitHub()
            publisher = DraftPublisher(
                PublicationStore(store.path),
                github,
                TrustedFinalizer(ControlledValidation()),
            )
            pipeline = CodingPublicationReconciler(
                store,
                publisher,
                {"repo": profile},
                {"test/repo": repo},
                {"test/repo": "master"},
            )
            for message in ("push succeeded", "PR created"):
                with pytest.raises(OSError, match=message):
                    pipeline.publish_job(job.id)
                assert await plane.dispatch_next() is None
            pr = pipeline.publish_job(job.id)
            assert pr["isDraft"]
            assert pipeline.publish_job(job.id) == pr
            assert github.pushes == github.creates == provider.starts == 1
            assert len(store.list_jobs()) == len(store.list_runs()) == 1
            store.observe_github_issue(replace(issue, state="closed"), policy)
            with pytest.raises(Exception, match="authorization changed"):
                pipeline.publish_job(job.id)
        finally:
            await client.close()
            await server.close()
            journal.close()
            store.close()

    asyncio.run(scenario())
