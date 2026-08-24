from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from agentd.agent_api import (
    AgentAction,
    AgentAPI,
    AgentAuthenticationError,
    AgentRequestKind,
    AgentRunStateError,
)
from agentd.domain.enums import JobState, RunState
from agentd.domain.models import (
    Checkpoint,
    DriverSession,
    ExecutionContract,
    Job,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    ResourceVector,
    ResumeCapsule,
    RunHandle,
    RunRecord,
    WorkerNode,
    WorkspaceLease,
)
from agentd.domain.transitions import initial_transition, transition_job
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore

FIXED_TIME = datetime(2026, 4, 1, 12, tzinfo=UTC)


class RecordingCoordinator:
    """Small lifecycle double behind the real ControlPlane facade."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []

    def execution_contract(self, run_id: str) -> ExecutionContract:
        self.calls.append(("assignment", run_id))
        return self.store.get_run(run_id).contract

    def is_managed_run(self, run_id: str) -> bool:
        try:
            self.store.get_driver_session(run_id)
        except LookupError:
            return False
        return True

    async def checkpoint(
        self,
        job_id: str,
        capsule: ResumeCapsule,
    ) -> Checkpoint:
        run = self._active_run(job_id)
        self.calls.append(("checkpoint", job_id))
        checkpoint = Checkpoint(
            id=f"checkpoint-{len(self.store.list_checkpoints(job_id)) + 1}",
            job_id=job_id,
            run_id=run.id,
            capsule=capsule,
            created_at=FIXED_TIME,
        )
        self.store.save_checkpoint(checkpoint)
        return checkpoint

    async def request_review(self, job_id: str) -> Job:
        self.calls.append(("request_review", job_id))
        job = self.store.get_job(job_id)
        review, event = transition_job(
            job,
            JobState.REVIEW,
            "worker requested review",
            at=FIXED_TIME,
        )
        self.store.save_job(review, event, expected=job)
        run = self._active_run(job_id)
        self.store.save_run(
            replace(run, state=RunState.SUSPENDED, ended_at=FIXED_TIME),
            expected=run,
        )
        return review

    async def complete(self, job_id: str) -> Job:
        self.calls.append(("complete", job_id))
        job = self.store.get_job(job_id)
        completed, event = transition_job(
            job,
            JobState.COMPLETED,
            "worker completion accepted",
            at=FIXED_TIME + timedelta(seconds=1),
        )
        self.store.save_job(completed, event, expected=job)
        run = self.store.latest_run(job_id)
        if run is None:  # pragma: no cover - test-double invariant
            raise AssertionError(f"No run for {job_id}")
        self.store.save_run(
            replace(
                run,
                state=RunState.COMPLETED,
                ended_at=FIXED_TIME + timedelta(seconds=1),
            ),
            expected=run,
        )
        return completed

    def _active_run(self, job_id: str) -> RunRecord:
        run = self.store.find_active_run(job_id)
        if run is None:  # pragma: no cover - test-double invariant
            raise AssertionError(f"No active run for {job_id}")
        return run


@dataclass(slots=True)
class APIContext:
    store: SQLiteStateStore
    coordinator: RecordingCoordinator
    control_plane: ControlPlane
    api: AgentAPI
    job: Job
    run: RunRecord
    contract: ExecutionContract


@pytest.fixture
def api_context(make_job: Callable[..., Job]) -> Iterator[APIContext]:
    store = SQLiteStateStore()
    job = make_job(id="job-1", state=JobState.RUNNING)
    store.create_job(job, initial_transition(job, reason="running test job"))
    contract = ExecutionContract(
        job_id=job.id,
        objective="Implement only the assigned change",
        scope="Repository test scope",
        acceptance_criteria=("tests pass",),
        dependency_results={"dependency": "abc123"},
        role="implementation worker",
        allowed_filesystem_scope=("/worktree",),
        checkpoint_expectations="Checkpoint at safe boundaries.",
        coordination_mechanisms=(
            "get_assignment",
            "request_refinement",
            "report_blocker",
            "checkpoint",
            "request_review",
            "complete",
        ),
        completion_protocol="Commit, validate, report; do not merge.",
        working_directory="/worktree",
        environment={"AGENTD_SCOPE": "test"},
        model_class="standard",
    )
    run = RunRecord(
        id="run-current",
        job_id=job.id,
        node_id="private-node",
        workspace_id="private-workspace",
        reservation_id="private-reservation",
        allocation_id="private-allocation",
        driver="fake",
        backend="direct",
        contract=contract,
        handle=RunHandle(id="handle", driver="fake"),
        state=RunState.RUNNING,
        started_at=FIXED_TIME,
    )
    store.save_node(
        WorkerNode(
            id=run.node_id,
            labels={},
            capacity=ResourceVector(cpu=8, ram_gb=16),
            allocated=job.resources,
            harnesses=frozenset({run.driver}),
            updated_at=FIXED_TIME,
        )
    )
    store.save_quota_pool(
        QuotaPool(
            id=job.quota_budget.pool_id,
            provider="private",
            remaining=100,
            reserved=job.quota_budget.expected_path,
            updated_at=FIXED_TIME,
        )
    )
    store.save_allocation(
        ResourceAllocation(
            id=run.allocation_id,
            job_id=job.id,
            node_id=run.node_id,
            resources=job.resources,
            created_at=FIXED_TIME,
        )
    )
    store.save_reservation(
        QuotaReservation(
            id=run.reservation_id,
            job_id=job.id,
            pool_id=job.quota_budget.pool_id,
            amount=job.quota_budget.expected_path,
            created_at=FIXED_TIME,
        )
    )
    store.save_workspace(
        WorkspaceLease(
            id=run.workspace_id,
            job_id=job.id,
            repository=job.repository,
            branch="agentd/job-1",
            working_directory=contract.working_directory,
            base_ref="HEAD",
            created_at=FIXED_TIME,
        ),
        expected=None,
    )
    store.save_run(run, expected=None)
    coordinator = RecordingCoordinator(store)
    control_plane = ControlPlane(store, coordinator=coordinator)
    api = AgentAPI(
        control_plane,
        clock=lambda: FIXED_TIME,
        id_factory=_id_factory("request"),
    )
    yield APIContext(store, coordinator, control_plane, api, job, run, contract)
    store.close()


def _id_factory(prefix: str) -> Callable[[], str]:
    sequence = iter(range(1, 10_000))
    return lambda: f"{prefix}-{next(sequence)}"


def test_get_assignment_authenticates_by_run_and_omits_scheduler_internals(
    api_context: APIContext,
) -> None:
    assignment = api_context.api.get_assignment(api_context.run.id)

    assert assignment == api_context.contract
    assert set(assignment.to_dict()).isdisjoint(
        {
            "node_id",
            "reservation_id",
            "allocation_id",
            "quota_budget",
            "priority",
            "qos",
            "resources",
            "selected_harness",
        }
    )
    assignment.environment["MUTATED"] = "yes"
    assert (
        "MUTATED" not in api_context.api.get_assignment(api_context.run.id).environment
    )

    with pytest.raises(AgentAuthenticationError, match="unknown or inactive"):
        api_context.api.get_assignment("not-a-run")


def test_stale_run_cannot_control_a_newer_attempt(api_context: APIContext) -> None:
    stale = replace(
        api_context.run,
        id="run-stale",
        handle=RunHandle(id="stale-handle", driver="fake"),
        state=RunState.SUSPENDED,
        started_at=FIXED_TIME - timedelta(hours=1),
        ended_at=FIXED_TIME - timedelta(minutes=30),
    )
    api_context.store.save_run(stale, expected=None)

    # The opaque capability can still retrieve its immutable historical contract,
    # but it cannot act on the current attempt for the same job.
    assert api_context.api.get_assignment(stale.id).job_id == api_context.job.id
    with pytest.raises(AgentAuthenticationError, match="unknown or inactive"):
        api_context.api.request_refinement(stale.id, "Can I change scope?")

    async def scenario() -> None:
        with pytest.raises(AgentAuthenticationError, match="unknown or inactive"):
            await api_context.api.checkpoint(stale.id, ResumeCapsule())

    asyncio.run(scenario())
    assert ("checkpoint", api_context.job.id) not in api_context.coordinator.calls


def test_refinement_and_blocker_records_are_validated_bounded_and_auditable(
    api_context: APIContext,
) -> None:
    api = AgentAPI(
        api_context.control_plane,
        max_request_records=2,
        max_message_length=40,
        clock=lambda: FIXED_TIME,
        id_factory=_id_factory("audit"),
    )

    first = api.request_refinement(api_context.run.id, "  Which module owns this? ")
    blocker = api.report_blocker(
        api_context.run.id,
        "Missing fixture",
        retryable=False,
    )
    latest = api.request_refinement(api_context.run.id, "May I add a local fake?")

    assert first.kind is AgentRequestKind.REFINEMENT
    assert first.message == "Which module owns this?"
    assert blocker.kind is AgentRequestKind.BLOCKER
    assert blocker.retryable is False
    assert latest.sequence == 3
    assert api.request_history(api_context.run.id) == (blocker, latest)

    with pytest.raises(ValueError, match="cannot be empty"):
        api.request_refinement(api_context.run.id, "  ")
    with pytest.raises(ValueError, match="configured limit"):
        api.report_blocker(api_context.run.id, "x" * 41)
    with pytest.raises(AgentAuthenticationError, match="unknown or inactive"):
        api.request_history("not-a-run")


def test_checkpoint_review_and_complete_delegate_without_leaking_job_fields(
    api_context: APIContext,
) -> None:
    capsule = ResumeCapsule(
        completed=("schema",),
        current=("retry handling",),
        next_steps=("run tests",),
        commit="abc123",
    )

    async def scenario() -> None:
        checkpoint = await api_context.api.checkpoint(api_context.run.id, capsule)
        assert checkpoint.checkpoint_id == "checkpoint-1"
        assert checkpoint.run_id == api_context.run.id
        assert api_context.control_plane.checkpoints(api_context.job.id)[0].capsule == (
            capsule
        )

        review = await api_context.api.request_review(api_context.run.id)
        assert review.action is AgentAction.REVIEW_REQUESTED
        assert review.state is JobState.REVIEW
        assert {field.name for field in fields(review)} == {"run_id", "action", "state"}

        with pytest.raises(AgentRunStateError, match="unavailable"):
            api_context.api.report_blocker(api_context.run.id, "late blocker")

        completed = await api_context.api.complete(api_context.run.id)
        assert completed.action is AgentAction.COMPLETE
        assert completed.state is JobState.COMPLETED
        assert {field.name for field in fields(completed)} == {
            "run_id",
            "action",
            "state",
        }

    asyncio.run(scenario())

    assert api_context.coordinator.calls[-3:] == [
        ("checkpoint", api_context.job.id),
        ("request_review", api_context.job.id),
        ("complete", api_context.job.id),
    ]
    assert [event.to_state for event in api_context.control_plane.history("job-1")] == [
        JobState.RUNNING,
        JobState.REVIEW,
        JobState.COMPLETED,
    ]


def test_agent_api_validates_bounded_request_configuration(
    api_context: APIContext,
) -> None:
    with pytest.raises(ValueError, match="capacity"):
        AgentAPI(api_context.control_plane, max_request_records=0)
    with pytest.raises(ValueError, match="message length"):
        AgentAPI(api_context.control_plane, max_message_length=0)


def test_managed_run_cannot_bypass_observation_review_gate(
    api_context: APIContext,
) -> None:
    api_context.store.save_driver_session(
        DriverSession(
            id="managed-session",
            run_id=api_context.run.id,
            driver=api_context.run.driver,
        ),
        expected=None,
    )

    async def scenario() -> None:
        with pytest.raises(AgentRunStateError, match="observation reconciliation"):
            await api_context.api.request_review(api_context.run.id)
        with pytest.raises(AgentRunStateError, match="observation reconciliation"):
            await api_context.api.complete(api_context.run.id)

    asyncio.run(scenario())
    assert api_context.coordinator.calls == []
    assert api_context.store.get_job(api_context.job.id).state is JobState.RUNNING
