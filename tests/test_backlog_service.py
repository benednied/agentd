import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.models import RepositoryProfile
from agentd.domain.enums import JobState, QuotaUnit
from agentd.domain.models import EffortEstimate, QuotaBudget, QuotaPool
from agentd.domain.transitions import transition_job
from agentd.intake.backlog import BacklogNode, BacklogSnapshot, ReadBound
from agentd.intake.backlog_service import BacklogLedger, BacklogReconciler
from agentd.intake.integration import IntegrationDecision, IntegrationStatus
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.service import GitHubIntake
from agentd.service import ControlPlane
from agentd.state.base import ConcurrentStateError
from agentd.state.sqlite import SQLiteStateStore


def _issue(number: int) -> SourceIssue:
    return SourceIssue(
        "acme/app",
        7,
        number,
        f"I_{number}",
        f"Issue {number}",
        "intent",
        "2026-09-20T12:00:00Z",
        labels=("agentd:approved",),
    )


def _graph(*, members=(78, 80), complete=True, blockers=None) -> BacklogSnapshot:
    blockers = blockers or {78: (79,), 80: (79,)}
    issues = {_issue(number).key: _issue(number) for number in (77, 78, 79, 80)}
    nodes = tuple(
        BacklogNode(
            issues[key],
            tuple(
                issues[f"github:7:I_{number}"].key
                for number in blockers.get(number, ())
            ),
        )
        for key, number in sorted((key, int(key.rsplit("_", 1)[1])) for key in issues)
    )
    order = tuple(node.id for node in nodes)
    return BacklogSnapshot(
        "acme/app",
        7,
        nodes,
        order,
        ReadBound.EXACT,
        complete,
        pages_read=1,
        page_limit=10,
        members=tuple(issues[f"github:7:I_{number}"].key for number in members),
        error=None if complete else "provider unavailable",
    )


class FakeStore:
    def __init__(self, path=":memory:"):
        self.path = path

    def backlog_binding(self, job_id):
        return None

    def list_runs(self, job_id=None):
        return []


class FakeIntake:
    store = FakeStore()
    policies = {"acme/app": IntakePolicy("acme/app", 7)}  # noqa: RUF012


@dataclass
class FakeIntegration:
    decisions: dict[int, IntegrationDecision]
    target_calls: int = 0

    def target_commit(self, repository, branch):
        self.target_calls += 1
        return "a" * 40

    def observe(self, issue, **kwargs):
        return self.decisions[issue.number]


def _decision(number: int, *, ready=False, links=(), reason="blocked"):
    return IntegrationDecision(
        IntegrationStatus.READY if ready else IntegrationStatus.BLOCKED,
        reason,
        links=tuple(links),
    )


def _reconciler(tmp_path, snapshot, integration, selection=None):
    reconciler = BacklogReconciler.__new__(BacklogReconciler)
    reconciler.intake = FakeIntake()
    reconciler.intake.store = FakeStore(tmp_path / "fake.sqlite")
    reconciler.source = type(
        "Source", (), {"discover": lambda self, *a, **k: snapshot}
    )()
    reconciler.integration = integration
    reconciler.config = {
        "profile": {"repository": "acme/app"},
        "repository_id": 7,
        "base_branch": "main",
    }
    reconciler.repository = "acme/app"
    reconciler.selection = selection or {"issues": [78, 80]}
    reconciler.ledger = BacklogLedger(
        tmp_path / "backlog.sqlite", {"repository": "acme/app", **reconciler.selection}
    )
    return reconciler


def test_incomplete_read_never_calls_provider_or_plans_admission(tmp_path):
    integration = FakeIntegration({})
    reconciler = _reconciler(tmp_path, _graph(complete=False), integration)
    plan = reconciler.plan(reconciler.discover())
    assert not plan["complete"]
    assert plan["nodes"] == []
    assert integration.target_calls == 0


def test_exact_grant_survives_restart_but_changed_edges_revoke(tmp_path):
    snapshot = _graph()
    ledger = BacklogLedger(
        tmp_path / "backlog.sqlite", {"repository": "acme/app", "issues": [78, 80]}
    )
    ledger.approve(snapshot, actor="operator", mode="exact")
    restarted = BacklogLedger(
        tmp_path / "backlog.sqlite", {"repository": "acme/app", "issues": [78, 80]}
    )
    assert restarted.authorized(snapshot, "github:7:I_78")
    changed = _graph(blockers={78: (), 80: (79,)})
    assert not restarted.authorized(changed, "github:7:I_78")
    assert not restarted.authorized(snapshot, "github:7:I_79")


def test_plan_marks_existing_pr_and_product_decision_without_coding(tmp_path):
    snapshot = _graph()
    integration = FakeIntegration(
        {
            77: _decision(77),
            78: _decision(78, links=(42,), reason="open PR"),
            79: _decision(79, ready=True),
            80: _decision(80),
        }
    )
    reconciler = _reconciler(
        tmp_path,
        snapshot,
        integration,
        {"issues": [78, 80], "product_decisions": {"80": "pending"}},
    )
    reconciler.ledger.approve(snapshot, actor="operator")
    plan = reconciler.plan(snapshot)
    by_issue = {item["issue"]: item for item in plan["nodes"]}
    assert by_issue[78]["reason"].startswith("existing_pull_request")
    assert by_issue[80]["reason"] == "product_decision_required"
    assert not any(item["ready"] for item in plan["nodes"])


def test_removed_member_is_not_authorized(tmp_path):
    snapshot = _graph(members=(78,))
    ledger = BacklogLedger(
        tmp_path / "backlog.sqlite", {"repository": "acme/app", "issues": [78]}
    )
    ledger.approve(snapshot, actor="operator")
    assert ledger.authorized(snapshot, "github:7:I_78")
    assert not ledger.authorized(snapshot, "github:7:I_80")


class PollSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def discover(self, repository, **kwargs):
        self.calls += 1
        return self.snapshot


class PollIntegration(FakeIntegration):
    def __init__(self, decisions, commits=("a" * 40,)):
        super().__init__(decisions)
        self.commits = list(commits)

    def target_commit(self, repository, branch):
        self.target_calls += 1
        return self.commits[-1]


def _real_reconciler(tmp_path: Path, snapshot, integration, source=None):
    store = SQLiteStateStore(tmp_path / "state.sqlite")
    store.register_quota_pool(QuotaPool("pool", "codex", 1000, unit=QuotaUnit.TOKENS))
    profile = RepositoryProfile(
        "app",
        "1",
        "acme/app",
        "https://github.com/acme/app.git",
        validation_commands=(("git", "diff", "--check"),),
    )
    compiler = CodingJobCompiler(
        profile,
        "a" * 40,
        QuotaBudget(10, maximum=100, pool_id="pool", unit=QuotaUnit.TOKENS),
        EffortEstimate(1, 2),
    )
    intake = GitHubIntake(
        store, object(), (IntakePolicy("acme/app", 7),), compiler, ControlPlane(store)
    )
    config = {
        "profile": profile.to_dict(),
        "repository_id": 7,
        "base_branch": "main",
        "base_commit": "a" * 40,
        "backlog": {"issues": [78]},
    }
    reconciler = BacklogReconciler(
        intake, source or PollSource(snapshot), integration, config
    )
    return reconciler, store


def test_real_poll_prerequisite_unlocks_one_ready_job_and_restart_is_idempotent(
    tmp_path,
):
    blocked = _graph(members=(78,))
    integration = PollIntegration(
        {
            77: _decision(77),
            78: _decision(78),
            79: _decision(79),
            80: _decision(80),
        }
    )
    source = PollSource(blocked)
    reconciler, store = _real_reconciler(tmp_path, blocked, integration, source)
    reconciler.ledger.approve(blocked, actor="operator")
    # The prerequisite is represented as not integrated, so no child is admitted.
    integration.decisions[79] = _decision(79)
    assert asyncio.run(reconciler.poll()) == ("github:7:I_78",)
    assert store.list_jobs() == []
    integration.decisions[79] = _decision(79, ready=True)
    integration.commits.append("b" * 40)
    asyncio.run(reconciler.poll())
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].state.value == "READY"
    assert jobs[0].base_ref == "b" * 40
    asyncio.run(reconciler.poll())
    assert len(store.list_jobs()) == 1
    store.close()


def test_real_poll_graph_change_fences_and_preserves_snapshot_on_failure(
    tmp_path,
):
    snapshot = _graph(members=(78,))
    integration = PollIntegration(
        {
            77: _decision(77),
            78: _decision(78),
            79: _decision(79, ready=True),
            80: _decision(80),
        }
    )
    source = PollSource(snapshot)
    reconciler, store = _real_reconciler(tmp_path, snapshot, integration, source)
    reconciler.ledger.approve(snapshot, actor="operator")
    asyncio.run(reconciler.poll())
    assert len(store.list_jobs()) == 1
    changed = _graph(members=(80,), blockers={78: (79,), 80: (79,)})
    source.snapshot = changed
    asyncio.run(reconciler.poll())
    assert store.list_jobs()[0].state.value == "BACKLOG"
    old = reconciler.ledger.grant()
    assert old is not None
    source.discover = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
    with suppress(OSError):
        asyncio.run(reconciler.poll())
    assert reconciler.ledger.grant() == old
    assert reconciler.ledger.status()["reason"] == "source_unavailable"
    store.close()


def test_stale_gate_rejects_admission_from_real_store(tmp_path):
    snapshot = _graph(members=(78,))
    integration = PollIntegration(
        {
            77: _decision(77),
            78: _decision(78),
            79: _decision(79, ready=True),
            80: _decision(80),
        }
    )
    reconciler, store = _real_reconciler(tmp_path, snapshot, integration)
    reconciler.ledger.approve(snapshot, actor="operator")
    import asyncio

    asyncio.run(reconciler.poll())
    job = store.list_jobs()[0]
    gate = store.backlog_gate(job.id)
    assert gate is not None
    store.set_backlog_gate(
        job.id,
        graph_revision=gate["graph_revision"],
        ready=True,
        base_commit=job.base_ref,
        reason="expired",
        checked_at=gate["checked_at"],
        valid_until="2000-01-01T00:00:00+00:00",
    )
    admitted, event = transition_job(job, JobState.ADMITTED, "selected")
    with pytest.raises(ConcurrentStateError):
        store.save_job(admitted, event, expected=job)
    assert store.get_job(job.id).state is JobState.READY
    store.close()
