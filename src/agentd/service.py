"""Small programmatic control-plane API.

Transport adapters should call this facade. Domain policy and external effects live
in the injected coordinator and managers rather than in HTTP, CLI, or an agent
harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from agentd.domain.enums import JobState, QoSClass
from agentd.domain.models import (
    Checkpoint,
    ExecutionContract,
    Job,
    QuotaPool,
    QuotaResetEvent,
    ResumeCapsule,
    RunRecord,
    StateTransition,
    WorkerNode,
    WorkspaceLease,
    new_id,
    utc_now,
)
from agentd.domain.transitions import initial_transition, transition_job
from agentd.runtime.quota import QuotaManager
from agentd.scheduling.reconnaissance import (
    ReconnaissanceOutcome,
    compile_reconnaissance,
    promote_hors_categorie,
)
from agentd.scheduling.tail import TailDecision, evaluate_tail
from agentd.state.base import StateStore


class ControlPlaneNotConfiguredError(RuntimeError):
    pass


class LifecycleCoordinator(Protocol):
    async def dispatch_next(self) -> RunRecord | None: ...

    async def checkpoint(self, job_id: str, capsule: ResumeCapsule) -> Checkpoint: ...

    async def suspend(self, job_id: str, capsule: ResumeCapsule) -> Job: ...

    async def resume(self, job_id: str) -> Job: ...

    async def cancel(self, job_id: str) -> Job: ...

    async def request_review(self, job_id: str) -> Job: ...

    async def complete(self, job_id: str) -> Job: ...

    def execution_contract(self, run_id: str) -> ExecutionContract: ...


class ControlPlane:
    """Application facade for commands and queries."""

    def __init__(
        self,
        store: StateStore,
        *,
        coordinator: LifecycleCoordinator | None = None,
        id_factory: Callable[[], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._quota = QuotaManager(store)
        self._id_factory = id_factory
        self._clock = clock

    @property
    def store(self) -> StateStore:
        return self._store

    def submit(self, job: Job) -> Job:
        if job.state != JobState.BACKLOG:
            raise ValueError("A submitted job must start in BACKLOG")
        self._store.create_job(job, initial_transition(job))
        target = (
            JobState.PLANNING if job.qos == QoSClass.HORS_CATEGORIE else JobState.READY
        )
        reason = (
            "hors-categorie job requires bounded reconnaissance"
            if target == JobState.PLANNING
            else "job accepted into the ready queue"
        )
        ready, event = transition_job(job, target, reason)
        self._store.save_job(ready, event)
        if target is JobState.PLANNING:
            reconnaissance = compile_reconnaissance(
                ready,
                reconnaissance_id=self._id_factory(),
                at=self._clock(),
            )
            self._store.create_job(
                reconnaissance,
                initial_transition(
                    reconnaissance,
                    reason=f"bounded reconnaissance for {ready.id}",
                ),
            )
            queued, queued_event = transition_job(
                reconnaissance,
                JobState.READY,
                "bounded reconnaissance slice queued",
            )
            self._store.save_job(queued, queued_event)
        return ready

    def reconnaissance_for(self, job_id: str) -> list[Job]:
        return [
            job for job in self._store.list_jobs() if job.reconnaissance_for == job_id
        ]

    def promote_hors_categorie(
        self,
        job_id: str,
        outcome: ReconnaissanceOutcome,
        *,
        qos: QoSClass = QoSClass.NORMAL,
    ) -> Job:
        parent = self._store.get_job(job_id)
        if parent.state is not JobState.PLANNING:
            raise ValueError(f"Job {job_id} is not awaiting reconnaissance")
        slices = self.reconnaissance_for(job_id)
        if not slices or any(job.state is not JobState.COMPLETED for job in slices):
            raise ValueError(
                "Every reconnaissance slice must complete before promotion"
            )
        refined = promote_hors_categorie(
            parent,
            outcome,
            at=self._clock(),
            qos=qos,
        )
        ready, event = transition_job(
            refined,
            JobState.READY,
            "reconnaissance bounded the job for normal execution",
        )
        self._store.save_job(ready, event)
        return ready

    def inspect_job(self, job_id: str) -> Job:
        return self._store.get_job(job_id)

    def list_jobs(self, states: frozenset[JobState] | None = None) -> list[Job]:
        return self._store.list_jobs(states)

    def history(self, job_id: str) -> list[StateTransition]:
        return self._store.list_transitions(job_id)

    def register_node(self, node: WorkerNode) -> WorkerNode:
        return self._store.register_node(node)

    def list_nodes(self) -> list[WorkerNode]:
        return self._store.list_nodes()

    def register_quota_pool(self, pool: QuotaPool) -> QuotaPool:
        return self._store.register_quota_pool(pool)

    def inspect_quota(self, pool_id: str) -> QuotaPool:
        return self._store.get_quota_pool(pool_id)

    def register_quota_event(self, event: QuotaResetEvent) -> QuotaPool:
        return self._quota.register_reset_event(event)

    def inspect_workspace(self, job_id: str) -> WorkspaceLease | None:
        return self._store.find_workspace(job_id)

    def runs(self, job_id: str) -> list[RunRecord]:
        return self._store.list_runs(job_id)

    def checkpoints(self, job_id: str) -> list[Checkpoint]:
        return self._store.list_checkpoints(job_id)

    def return_to_backlog(self, job_id: str, *, reason: str = "throttled") -> Job:
        job = self._store.get_job(job_id)
        if job.state is not JobState.READY:
            raise ValueError(f"Only a ready job can return to backlog, not {job.state}")
        deferred, event = transition_job(job, JobState.BACKLOG, reason)
        self._store.save_job(deferred, event)
        return deferred

    def requeue(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state is not JobState.BACKLOG:
            raise ValueError(f"Only a backlog job can be requeued, not {job.state}")
        if job.qos is QoSClass.HORS_CATEGORIE:
            raise ValueError("Hors-categorie work must pass reconnaissance")
        ready, event = transition_job(
            job,
            JobState.READY,
            "job returned from backlog",
        )
        self._store.save_job(ready, event)
        return ready

    def degrade(self, job_id: str, *, harness: str, model_class: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state not in {
            JobState.BACKLOG,
            JobState.READY,
            JobState.SUSPENDED,
        }:
            raise ValueError(f"Job {job_id} cannot be degraded while {job.state}")
        if harness not in job.allowed_harnesses:
            raise ValueError(f"Harness {harness!r} is not allowed for job {job_id}")
        if not model_class.strip():
            raise ValueError("A degraded model class cannot be empty")
        preferred = (
            harness,
            *(item for item in job.preferred_harnesses if item != harness),
        )
        degraded = replace(
            job,
            preferred_harnesses=preferred,
            preferred_model_class=model_class,
            minimum_model_class=model_class,
            selected_harness=None,
            selected_model_class=None,
            updated_at=self._clock(),
        )
        self._store.save_job(degraded)
        return degraded

    def evaluate_tail(self, job_id: str, consumed: float) -> TailDecision:
        return evaluate_tail(self._store.get_job(job_id).effort, consumed)

    async def dispatch_next(self) -> RunRecord | None:
        return await self._lifecycle().dispatch_next()

    async def checkpoint(self, job_id: str, capsule: ResumeCapsule) -> Checkpoint:
        return await self._lifecycle().checkpoint(job_id, capsule)

    async def pause(self, job_id: str, capsule: ResumeCapsule) -> Job:
        return await self._lifecycle().suspend(job_id, capsule)

    async def resume(self, job_id: str) -> Job:
        return await self._lifecycle().resume(job_id)

    async def cancel(self, job_id: str) -> Job:
        return await self._lifecycle().cancel(job_id)

    async def request_review(self, job_id: str) -> Job:
        return await self._lifecycle().request_review(job_id)

    async def complete(self, job_id: str) -> Job:
        return await self._lifecycle().complete(job_id)

    def assignment(self, run_id: str) -> ExecutionContract:
        return self._lifecycle().execution_contract(run_id)

    def _lifecycle(self) -> LifecycleCoordinator:
        if self._coordinator is None:
            raise ControlPlaneNotConfiguredError(
                "This ControlPlane has no lifecycle coordinator"
            )
        return self._coordinator
