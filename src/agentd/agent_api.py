"""Minimal model-facing API, independent of scheduler policy and transports.

The run ID is an opaque bearer capability. Mutating calls additionally require it
to name the job's current run, preventing an old worker attempt from controlling a
newer attempt for the same job.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock

from agentd.domain.enums import JobState, RunState
from agentd.domain.models import (
    Checkpoint,
    ExecutionContract,
    Job,
    ResumeCapsule,
    RunRecord,
    new_id,
    utc_now,
)
from agentd.service import ControlPlane


class AgentAuthenticationError(PermissionError):
    """Raised when a run capability is unknown, stale, or no longer current."""


class AgentRunStateError(RuntimeError):
    """Raised when an authenticated run cannot perform an operation now."""


class AgentAPIInvariantError(RuntimeError):
    """Raised when the control plane returns data for a different run or job."""


class AgentRequestKind(StrEnum):
    REFINEMENT = "refinement"
    BLOCKER = "blocker"


class AgentAction(StrEnum):
    REVIEW_REQUESTED = "review-requested"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class AgentRequestRecord:
    """A bounded in-process audit record for requests without a durable port."""

    request_id: str
    sequence: int
    run_id: str
    kind: AgentRequestKind
    message: str
    created_at: datetime
    retryable: bool | None = None


@dataclass(frozen=True, slots=True)
class AgentActionResult:
    """Compact lifecycle result that intentionally omits scheduler internals."""

    run_id: str
    action: AgentAction
    state: JobState


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Acknowledgement that a resume capsule is durable for this run."""

    checkpoint_id: str
    run_id: str
    created_at: datetime


class AgentAPI:
    """Worker-scoped protocol facade over the authoritative ``ControlPlane``.

    Refinement and blocker requests have no generic durable event repository in
    the MVP. They are therefore returned to the caller and retained in a bounded,
    timestamped in-memory log. Checkpoints and lifecycle actions continue through
    the control plane and use its durable audit mechanisms.
    """

    def __init__(
        self,
        control_plane: ControlPlane,
        *,
        max_request_records: int = 256,
        max_message_length: int = 4_000,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        if max_request_records <= 0:
            raise ValueError("Request-record capacity must be positive")
        if max_message_length <= 0:
            raise ValueError("Maximum message length must be positive")
        self._control_plane = control_plane
        self._max_message_length = max_message_length
        self._clock = clock
        self._id_factory = id_factory
        self._request_records: deque[AgentRequestRecord] = deque(
            maxlen=max_request_records
        )
        self._next_sequence = 1
        self._records_lock = RLock()

    def get_assignment(self, run_id: str) -> ExecutionContract:
        """Return only the worker execution contract for an authenticated run."""

        run = self._known_run(run_id)
        contract = self._control_plane.assignment(run_id)
        if contract.job_id != run.job_id:
            raise AgentAPIInvariantError("Assignment does not belong to this run")
        # ExecutionContract is frozen but contains dictionaries. Defensive copies
        # keep a caller from mutating a control-plane-owned in-memory instance.
        return replace(
            contract,
            dependency_results=dict(contract.dependency_results),
            environment=dict(contract.environment),
        )

    def request_refinement(self, run_id: str, question: str) -> AgentRequestRecord:
        """Record a concrete question for the assignment owner/coordinator."""

        self._require_current_run(run_id, frozenset({RunState.RUNNING}))
        return self._record_request(
            run_id,
            AgentRequestKind.REFINEMENT,
            question,
            retryable=None,
        )

    def report_blocker(
        self,
        run_id: str,
        blocker: str,
        *,
        retryable: bool = True,
    ) -> AgentRequestRecord:
        """Record an actionable blocker without exposing scheduling decisions."""

        self._require_current_run(run_id, frozenset({RunState.RUNNING}))
        return self._record_request(
            run_id,
            AgentRequestKind.BLOCKER,
            blocker,
            retryable=retryable,
        )

    async def checkpoint(
        self,
        run_id: str,
        capsule: ResumeCapsule,
    ) -> CheckpointResult:
        """Persist a compact resume capsule for the authenticated current run."""

        run = self._require_current_run(
            run_id,
            frozenset({RunState.RUNNING, RunState.DRAINING}),
        )
        checkpoint = await self._control_plane.checkpoint(run.job_id, capsule)
        self._verify_checkpoint(checkpoint, run)
        return CheckpointResult(
            checkpoint_id=checkpoint.id,
            run_id=checkpoint.run_id,
            created_at=checkpoint.created_at,
        )

    async def request_review(self, run_id: str) -> AgentActionResult:
        """Hand the current artifact to the control plane's review lifecycle."""

        run = self._require_current_run(run_id, frozenset({RunState.RUNNING}))
        job = await self._control_plane.request_review(run.job_id)
        self._verify_job(job, run)
        return AgentActionResult(
            run_id=run_id,
            action=AgentAction.REVIEW_REQUESTED,
            state=job.state,
        )

    async def complete(self, run_id: str) -> AgentActionResult:
        """Report completion through the control plane's accepted lifecycle."""

        run = self._require_current_run(
            run_id,
            frozenset({RunState.RUNNING, RunState.SUSPENDED}),
        )
        if (
            run.state is RunState.SUSPENDED
            and self._control_plane.inspect_job(run.job_id).state is not JobState.REVIEW
        ):
            raise AgentRunStateError(
                "A suspended assignment must resume before completion"
            )
        job = await self._control_plane.complete(run.job_id)
        self._verify_job(job, run)
        return AgentActionResult(
            run_id=run_id,
            action=AgentAction.COMPLETE,
            state=job.state,
        )

    def request_history(self, run_id: str) -> tuple[AgentRequestRecord, ...]:
        """Return only this authenticated run's retained in-memory requests."""

        self._known_run(run_id)
        with self._records_lock:
            return tuple(
                record for record in self._request_records if record.run_id == run_id
            )

    def _known_run(self, run_id: str) -> RunRecord:
        if not run_id.strip():
            raise AgentAuthenticationError("Run capability is unknown or inactive")
        try:
            return self._control_plane.store.get_run(run_id)
        except LookupError as error:
            raise AgentAuthenticationError(
                "Run capability is unknown or inactive"
            ) from error

    def _require_current_run(
        self,
        run_id: str,
        allowed_states: frozenset[RunState],
    ) -> RunRecord:
        run = self._known_run(run_id)
        current = self._control_plane.store.find_active_run(run.job_id)
        is_active_attempt = current is not None and current.id == run.id
        latest = self._control_plane.store.latest_run(run.job_id)
        job = self._control_plane.inspect_job(run.job_id)
        is_review_attempt = (
            run.state is RunState.SUSPENDED
            and job.state is JobState.REVIEW
            and latest is not None
            and latest.id == run.id
        )
        if not is_active_attempt and not is_review_attempt:
            raise AgentAuthenticationError("Run capability is unknown or inactive")
        if run.state not in allowed_states:
            raise AgentRunStateError("Operation is unavailable for this run state")
        return run

    def _record_request(
        self,
        run_id: str,
        kind: AgentRequestKind,
        message: str,
        *,
        retryable: bool | None,
    ) -> AgentRequestRecord:
        normalized = message.strip()
        if not normalized:
            raise ValueError("Agent request message cannot be empty")
        if len(normalized) > self._max_message_length:
            raise ValueError("Agent request message exceeds the configured limit")
        with self._records_lock:
            record = AgentRequestRecord(
                request_id=self._id_factory(),
                sequence=self._next_sequence,
                run_id=run_id,
                kind=kind,
                message=normalized,
                created_at=self._clock(),
                retryable=retryable,
            )
            self._next_sequence += 1
            self._request_records.append(record)
        return record

    @staticmethod
    def _verify_checkpoint(checkpoint: Checkpoint, run: RunRecord) -> None:
        if checkpoint.run_id != run.id or checkpoint.job_id != run.job_id:
            raise AgentAPIInvariantError("Checkpoint does not belong to this run")

    @staticmethod
    def _verify_job(job: Job, run: RunRecord) -> None:
        if job.id != run.job_id:
            raise AgentAPIInvariantError("Lifecycle result does not belong to this run")


__all__ = [
    "AgentAPI",
    "AgentAPIInvariantError",
    "AgentAction",
    "AgentActionResult",
    "AgentAuthenticationError",
    "AgentRequestKind",
    "AgentRequestRecord",
    "AgentRunStateError",
    "CheckpointResult",
]
