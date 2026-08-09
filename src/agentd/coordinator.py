"""Effectful scheduler coordinator for the local MVP lifecycle."""

from __future__ import annotations

from dataclasses import replace

from agentd.domain.enums import (
    JobState,
    PreemptionPolicy,
    QuotaMode,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    Checkpoint,
    ExecutionContract,
    Job,
    QuotaReservation,
    ResumeCapsule,
    RunHandle,
    RunRecord,
    RunResult,
    WorkspaceLease,
    new_id,
    utc_now,
)
from agentd.domain.transitions import transition_job
from agentd.harness.registry import DriverRegistry
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.scheduling.burn import burn_order_key
from agentd.scheduling.placement import Placement, compatible_placements
from agentd.scheduling.readiness import gang_readiness
from agentd.state.base import StateStore
from agentd.workers.protocol import WorkerBackend
from agentd.workers.registry import BackendRegistry
from agentd.workspaces.base import WorkspaceManager, WorkspaceReleaseError


class LifecycleError(RuntimeError):
    pass


class SchedulerCoordinator:
    """Apply pure scheduling decisions and compensate failed external effects."""

    def __init__(
        self,
        store: StateStore,
        workspace_manager: WorkspaceManager,
        drivers: DriverRegistry,
        *,
        quota_manager: QuotaManager | None = None,
        resource_manager: ResourceManager | None = None,
        backends: BackendRegistry | None = None,
    ) -> None:
        self._store = store
        self._workspaces = workspace_manager
        self._drivers = drivers
        self._quota = quota_manager or QuotaManager(store)
        self._resources = resource_manager or ResourceManager(store)
        self._backends = backends

    async def dispatch_next(self) -> RunRecord | None:
        jobs = self._store.list_jobs(frozenset({JobState.READY}))
        if not jobs:
            return None
        first_error: Exception | None = None
        all_jobs = self._store.list_jobs()
        states = {job.id: job.state for job in all_jobs}
        capabilities = {item.name: item for item in self._drivers.capabilities()}

        def candidate_key(job: Job) -> object:
            try:
                mode = self._store.get_quota_pool(job.quota_budget.pool_id).mode
            except LookupError:
                mode = QuotaMode.NORMAL
            return burn_order_key(job, mode)

        for job in sorted(jobs, key=candidate_key):
            if not gang_readiness(job, all_jobs, states).ready:
                continue
            try:
                self._store.get_quota_pool(job.quota_budget.pool_id)
            except LookupError:
                continue
            for placement in compatible_placements(
                job,
                self._store.list_nodes(),
                capabilities,
            ):
                backend = self._select_backend(placement.node_id)
                if self._backends is not None and backend is None:
                    continue
                try:
                    return await self._dispatch(job, placement, backend)
                except QuotaAdmissionError:
                    break
                except Exception as error:
                    # One malformed repository, workspace, or harness must not
                    # starve every lower-ranked runnable job. Preserve the first
                    # error for callers when no candidate can make progress.
                    if first_error is None:
                        first_error = error
                    break
        if first_error is not None:
            raise first_error
        return None

    async def _dispatch(
        self,
        job: Job,
        placement: Placement,
        backend: WorkerBackend | None,
    ) -> RunRecord:
        reservation: QuotaReservation | None = None
        allocation_id: str | None = None
        workspace: WorkspaceLease | None = None
        workspace_created = False
        admitted: Job | None = None
        handle: RunHandle | None = None
        run: RunRecord | None = None
        driver = self._drivers.get(placement.harness)

        try:
            reservation = self._quota.reserve(job)
            node = self._store.get_node(placement.node_id)
            allocation = self._resources.allocate(job, node)
            allocation_id = allocation.id

            workspace = self._store.find_workspace(job.id)
            if (
                workspace is not None
                and workspace.state is WorkspaceState.LEASED
                and not self._workspaces.is_available(workspace)
            ):
                self._store.save_workspace(
                    replace(workspace, state=WorkspaceState.FAILED)
                )
                workspace = None
            if workspace is None or workspace.state is not WorkspaceState.LEASED:
                workspace = self._workspaces.allocate(
                    job,
                    base_ref=self._base_ref(job),
                )
                self._store.save_workspace(workspace)
                workspace_created = True

            selected = replace(
                job,
                selected_harness=placement.harness,
                selected_model_class=placement.model_class,
            )
            candidate, event = transition_job(
                selected,
                JobState.ADMITTED,
                f"admitted on node {placement.node_id} with {placement.harness}",
            )
            self._store.save_job(candidate, event)
            admitted = candidate

            contract = self._build_contract(admitted, workspace)
            run = RunRecord(
                job_id=admitted.id,
                node_id=placement.node_id,
                workspace_id=workspace.id,
                reservation_id=reservation.id,
                allocation_id=allocation.id,
                driver=placement.harness,
                backend=(
                    backend.capabilities().name if backend is not None else "direct"
                ),
                contract=contract,
                handle=RunHandle(
                    id=f"pending-{new_id()}",
                    driver=placement.harness,
                ),
                state=RunState.STARTING,
            )
            self._store.save_run(run)
            handle = (
                await backend.dispatch(driver, contract)
                if backend is not None
                else await driver.start(contract)
            )
            # Persist the externally meaningful handle before publishing RUNNING.
            # A reconciler can now identify and stop a process even if the job/run
            # transition below is interrupted.
            run = replace(run, handle=handle)
            self._store.save_run(run)
            run = replace(run, state=RunState.RUNNING)
            running, running_event = transition_job(
                admitted,
                JobState.RUNNING,
                f"harness run {run.id} started",
            )
            self._store.save_job_and_run(running, running_event, run)
            return run
        except BaseException as error:
            cleanup_errors: list[BaseException] = []
            quiesced = handle is None
            if handle is not None:
                try:
                    await driver.cancel(handle)
                    await driver.collect(handle)
                    quiesced = True
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if quiesced:
                if workspace_created and workspace is not None:
                    try:
                        self._release_workspace(workspace, retain_on_failure=True)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                if allocation_id is not None:
                    try:
                        self._resources.release(allocation_id)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                if reservation is not None:
                    try:
                        self._quota.release(reservation.id, cancelled=True)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if admitted is not None and quiesced:
                ready, event = transition_job(
                    admitted,
                    JobState.READY,
                    "dispatch failed; admission resources compensated",
                )
                if run is not None:
                    cancelled_run = replace(
                        run,
                        handle=handle or run.handle,
                        state=RunState.CANCELLED,
                        ended_at=utc_now(),
                        result=RunResult(
                            outcome=RunOutcome.CANCELLED,
                            summary="dispatch failed before the run became active",
                        ),
                    )
                    self._store.save_job_and_run(ready, event, cancelled_run)
                else:
                    self._store.save_job(ready, event)
            for cleanup_error in cleanup_errors:
                error.add_note(f"Cleanup also failed: {cleanup_error}")
            raise

    async def checkpoint(
        self,
        job_id: str,
        capsule: ResumeCapsule,
    ) -> Checkpoint:
        job = self._store.get_job(job_id)
        if job.state not in {JobState.RUNNING, JobState.DRAINING}:
            raise LifecycleError(f"Job {job_id} is not running")
        run = self._require_active_run(job_id)
        checkpoint = Checkpoint(job_id=job_id, run_id=run.id, capsule=capsule)
        self._store.save_checkpoint(checkpoint)
        return checkpoint

    async def suspend(self, job_id: str, capsule: ResumeCapsule) -> Job:
        job = self._store.get_job(job_id)
        if job.state not in {
            JobState.RUNNING,
            JobState.DRAINING,
            JobState.CHECKPOINTED,
        }:
            raise LifecycleError(f"Job {job_id} cannot suspend from {job.state}")
        if job.preemption_policy is PreemptionPolicy.NEVER:
            raise LifecycleError(f"Job {job_id} does not permit preemption")
        run = self._require_active_run(job_id)
        driver = self._drivers.get(run.driver)

        if job.state is JobState.RUNNING:
            draining, event = transition_job(
                job,
                JobState.DRAINING,
                "suspension requested; draining to a safe checkpoint",
            )
            run = replace(run, state=RunState.DRAINING)
            self._store.save_job_and_run(draining, event, run)
            job = draining

        if job.state is JobState.DRAINING:
            await driver.steer(
                run.handle,
                "Stop at the next safe boundary and preserve the supplied "
                "resume state.",
            )
            if not driver.capabilities().native_pause:
                # Turn-boundary adapters (including Codex) deliver steering
                # during collection. Let that bounded turn finish before
                # claiming a durable safe checkpoint; interrupting here would
                # discard the queued instruction.
                result = run.result or await driver.collect(run.handle)
                result = self._result_with_workspace_commit(run, result)
                run = replace(run, result=result)
                self._store.save_run(run)
            capsule = self._capsule_with_workspace_commit(run, capsule)
            checkpoint = self._store.latest_checkpoint(job_id)
            if (
                checkpoint is None
                or checkpoint.run_id != run.id
                or checkpoint.capsule != capsule
            ):
                checkpoint = Checkpoint(
                    job_id=job_id,
                    run_id=run.id,
                    capsule=capsule,
                )
                self._store.save_checkpoint(checkpoint)
            checkpointed, event = transition_job(
                job,
                JobState.CHECKPOINTED,
                f"durable checkpoint {checkpoint.id} recorded",
            )
            run = replace(run, state=RunState.CHECKPOINTED)
            self._store.save_job_and_run(checkpointed, event, run)
            job = checkpointed

        result = run.result
        if result is None:
            await driver.interrupt(run.handle)
            result = await driver.collect(run.handle)
        result = self._result_with_workspace_commit(run, result)
        run = replace(run, result=result)
        # Record usage and the durable handoff before making capacity available.
        self._store.save_run(run)
        self._resources.release(run.allocation_id)
        self._quota.release(
            run.reservation_id,
            consumed=result.consumed_quota,
        )
        suspended, event = transition_job(
            job,
            JobState.SUSPENDED,
            "checkpoint durable; scarce execution resources released",
        )
        final_run = replace(run, state=RunState.SUSPENDED, ended_at=utc_now())
        self._store.save_job_and_run(suspended, event, final_run)
        return suspended

    async def resume(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state is not JobState.SUSPENDED:
            raise LifecycleError(f"Job {job_id} is not suspended")
        return self._transition(
            job,
            JobState.READY,
            "resume requested; queued for a new run attempt",
        )

    async def request_review(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state is not JobState.RUNNING:
            raise LifecycleError(f"Job {job_id} is not running")
        run = self._require_active_run(job_id)
        driver = self._drivers.get(run.driver)
        await driver.interrupt(run.handle)
        result = run.result or await driver.collect(run.handle)
        result = self._review_handoff_result(result)
        result = self._result_with_workspace_commit(run, result)
        run = replace(run, result=result)
        # The adapter is quiescent and its usage is durable before capacity is
        # released. Retrying this operation is therefore safe and idempotent.
        self._store.save_run(run)
        self._resources.release(run.allocation_id)
        self._quota.release(
            run.reservation_id,
            consumed=result.consumed_quota,
        )
        review, event = transition_job(
            job,
            JobState.REVIEW,
            f"run {run.id} handed off for review",
        )
        final_run = replace(run, state=RunState.SUSPENDED, ended_at=utc_now())
        self._store.save_job_and_run(review, event, final_run)
        return review

    async def complete(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.terminal:
            latest = self._store.latest_run(job_id)
            if latest is not None:
                self._raise_cleanup_errors(
                    self._cleanup_job_resources(
                        job_id,
                        latest,
                        completed=job.state is JobState.COMPLETED,
                    )
                )
            return job
        if job.state not in {JobState.RUNNING, JobState.REVIEW}:
            raise LifecycleError(f"Job {job_id} cannot complete from {job.state}")
        run = (
            self._require_active_run(job_id)
            if job.state is JobState.RUNNING
            else self._require_latest_run(job_id)
        )
        result = run.result
        if result is None:
            result = await self._drivers.get(run.driver).collect(run.handle)
            result = self._result_with_workspace_commit(run, result)
            run = replace(run, result=result)
            # Collect is an external effect. Persist it while the run remains
            # retryable, before attempting the atomic terminal transition.
            self._store.save_run(run)

        target = {
            RunOutcome.COMPLETED: JobState.COMPLETED,
            RunOutcome.FAILED: JobState.FAILED,
            RunOutcome.CANCELLED: JobState.CANCELLED,
        }[result.outcome]
        run_state = {
            RunOutcome.COMPLETED: RunState.COMPLETED,
            RunOutcome.FAILED: RunState.FAILED,
            RunOutcome.CANCELLED: RunState.CANCELLED,
        }[result.outcome]

        final_run = replace(
            run,
            state=run_state,
            ended_at=utc_now(),
            result=result,
        )
        completed, event = transition_job(
            job,
            target,
            f"run {run.id} reported {result.outcome.value}",
        )
        # The run and its audit transition are one SQLite transaction. No
        # external cleanup happens if this persistence step fails.
        self._store.save_job_and_run(completed, event, final_run)
        self._raise_cleanup_errors(
            self._cleanup_job_resources(
                job_id,
                final_run,
                completed=target is JobState.COMPLETED,
            )
        )
        return completed

    async def cancel(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        run = self._store.find_active_run(job_id)
        if run is None:
            run = self._store.latest_run(job_id)

        if job.terminal:
            self._raise_cleanup_errors(
                self._cleanup_job_resources(job_id, run, completed=False)
            )
            return job

        collected: RunResult | None = run.result if run is not None else None
        if run is not None and run.state in {
            RunState.STARTING,
            RunState.RUNNING,
            RunState.DRAINING,
            RunState.CHECKPOINTED,
        }:
            driver = self._drivers.get(run.driver)
            try:
                await driver.cancel(run.handle)
                collected = await driver.collect(run.handle)
            except BaseException as error:
                raise LifecycleError(
                    f"Could not quiesce run {run.id}; resources remain allocated"
                ) from error

        result = RunResult(
            outcome=RunOutcome.CANCELLED,
            summary="cancelled by control plane",
            commit=collected.commit if collected is not None else None,
            consumed_quota=(collected.consumed_quota if collected is not None else 0),
            metadata=(
                {
                    **collected.metadata,
                    "cancelled_from_outcome": collected.outcome.value,
                }
                if collected is not None
                else {}
            ),
        )
        if run is not None:
            run = replace(run, result=result)
            self._store.save_run(run)

        cleanup_errors = self._cleanup_job_resources(
            job_id,
            run,
            completed=False,
            cancelled=True,
        )
        cancelled, event = transition_job(job, JobState.CANCELLED, "cancel requested")
        if run is not None:
            final_run = replace(
                run,
                state=RunState.CANCELLED,
                ended_at=utc_now(),
                result=result,
            )
            self._store.save_job_and_run(cancelled, event, final_run)
        else:
            self._store.save_job(cancelled, event)
        self._raise_cleanup_errors(cleanup_errors)
        return cancelled

    def execution_contract(self, run_id: str) -> ExecutionContract:
        return self._store.get_run(run_id).contract

    def _transition(self, job: Job, state: JobState, reason: str) -> Job:
        updated, event = transition_job(job, state, reason)
        self._store.save_job(updated, event)
        return updated

    def _require_active_run(self, job_id: str) -> RunRecord:
        run = self._store.find_active_run(job_id)
        if run is None:
            raise LifecycleError(f"Job {job_id} has no active run")
        return run

    def _require_latest_run(self, job_id: str) -> RunRecord:
        run = self._store.latest_run(job_id)
        if run is None:
            raise LifecycleError(f"Job {job_id} has no run attempt")
        return run

    def _build_contract(
        self,
        job: Job,
        workspace: WorkspaceLease,
    ) -> ExecutionContract:
        dependency_results: dict[str, str] = {}
        for dependency_id in job.dependencies:
            runs = self._store.list_runs(dependency_id)
            if not runs or runs[-1].result is None:
                continue
            result = runs[-1].result
            dependency_results[dependency_id] = result.commit or result.summary
        return ExecutionContract(
            job_id=job.id,
            objective=job.objective,
            scope=f"Project {job.project} in repository {job.repository}",
            acceptance_criteria=job.acceptance_criteria,
            dependency_results=dependency_results,
            role="implementation worker",
            allowed_filesystem_scope=(workspace.working_directory,),
            checkpoint_expectations=(
                "Return a compact resume capsule at safe boundaries and before "
                "preemption. Commit durable handoffs."
            ),
            coordination_mechanisms=(
                "get_assignment",
                "request_refinement",
                "report_blocker",
                "checkpoint",
                "request_review",
                "complete",
            ),
            completion_protocol=(
                "Satisfy the acceptance criteria, run validation, commit changes, "
                "and report completion to the control plane. Do not merge."
            ),
            working_directory=workspace.working_directory,
            environment=workspace.environment,
            model_class=job.selected_model_class or job.minimum_model_class,
            resume=(
                latest.capsule
                if (latest := self._store.latest_checkpoint(job.id)) is not None
                else None
            ),
        )

    def _base_ref(self, job: Job) -> str:
        latest = self._store.latest_checkpoint(job.id)
        if latest is not None and latest.capsule.commit:
            return latest.capsule.commit
        for dependency_id in reversed(job.dependencies):
            runs = self._store.list_runs(dependency_id)
            if runs and runs[-1].result and runs[-1].result.commit:
                return runs[-1].result.commit
        return "HEAD"

    def _capsule_with_workspace_commit(
        self,
        run: RunRecord,
        capsule: ResumeCapsule,
    ) -> ResumeCapsule:
        if capsule.commit is not None:
            return capsule
        workspace = self._store.get_workspace(run.workspace_id)
        return replace(capsule, commit=self._workspaces.current_commit(workspace))

    def _result_with_workspace_commit(
        self,
        run: RunRecord,
        result: RunResult,
    ) -> RunResult:
        if result.commit is not None:
            return result
        workspace = self._store.get_workspace(run.workspace_id)
        return replace(result, commit=self._workspaces.current_commit(workspace))

    @staticmethod
    def _review_handoff_result(result: RunResult) -> RunResult:
        if result.outcome is not RunOutcome.CANCELLED:
            return result
        return replace(
            result,
            outcome=RunOutcome.COMPLETED,
            metadata={
                **result.metadata,
                "review_handoff_outcome": RunOutcome.CANCELLED.value,
            },
        )

    def _cleanup_job_resources(
        self,
        job_id: str,
        run: RunRecord | None,
        *,
        completed: bool,
        cancelled: bool = False,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        workspace = self._store.find_workspace(job_id)
        if workspace is not None and workspace.state in {
            WorkspaceState.LEASED,
            WorkspaceState.RETAINED,
        }:
            try:
                self._release_workspace(workspace, retain_on_failure=True)
            except BaseException as error:
                errors.append(error)

        allocation = self._store.find_active_allocation(job_id)
        if allocation is not None:
            try:
                self._resources.release(allocation.id)
            except BaseException as error:
                errors.append(error)

        reservation = self._store.find_active_reservation(job_id)
        if reservation is not None:
            try:
                self._quota.release(
                    reservation.id,
                    consumed=(
                        run.result.consumed_quota
                        if run is not None and run.result is not None
                        else 0
                    ),
                    cancelled=cancelled,
                )
            except BaseException as error:
                errors.append(error)
        return errors

    @staticmethod
    def _raise_cleanup_errors(errors: list[BaseException]) -> None:
        if not errors:
            return
        error = LifecycleError("Lifecycle persisted, but one or more cleanups failed")
        for cleanup_error in errors:
            error.add_note(str(cleanup_error))
        raise error

    def _select_backend(self, node_id: str) -> WorkerBackend | None:
        if self._backends is None:
            return None
        compatible = self._backends.compatible(self._store.get_node(node_id))
        return compatible[0] if compatible else None

    def _release_workspace(
        self,
        workspace: WorkspaceLease,
        *,
        retain_on_failure: bool,
    ) -> WorkspaceLease:
        try:
            released = self._workspaces.release(workspace)
        except WorkspaceReleaseError:
            if not retain_on_failure:
                raise
            retained = replace(workspace, state=WorkspaceState.RETAINED)
            self._store.save_workspace(retained)
            return retained
        self._store.save_workspace(released)
        return released
