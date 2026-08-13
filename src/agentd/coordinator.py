"""Effectful scheduler coordinator for the local MVP lifecycle."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta

from agentd.domain.enums import (
    JobState,
    PreemptionPolicy,
    QoSClass,
    QuotaMode,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    Checkpoint,
    ExecutionContract,
    Job,
    ProviderQuotaSnapshot,
    QuotaReservation,
    ResumeCapsule,
    RunCommand,
    RunCommandAck,
    RunHandle,
    RunObservation,
    RunRecord,
    RunResult,
    WorkspaceLease,
    new_id,
    utc_now,
)
from agentd.domain.transitions import transition_job
from agentd.harness.protocol import (
    ContinuingManagedHarnessDriver,
    ManagedHarnessDriver,
    PendingCommandHarnessDriver,
)
from agentd.harness.registry import DriverRegistry
from agentd.observability import event_logger
from agentd.provisioning import RepositoryProvisioner
from agentd.runtime.accounts import (
    DEFAULT_ACCOUNT_POLICY,
    DEFAULT_JOB_USAGE_POLICY,
    AccountPolicyThresholds,
    JobUsagePolicy,
    hard_cap_interrupt_command,
    maximum_checkpoint_command,
    provider_allows_qos,
    provider_checkpoint_command,
    provider_quota_reached,
    provider_used_percent,
    quota_mode_for_snapshot,
    should_checkpoint_for_maximum,
    should_top_up,
    snapshot_is_stale,
)
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.scheduling.burn import burn_order_key
from agentd.scheduling.placement import Placement, compatible_placements
from agentd.scheduling.readiness import gang_readiness
from agentd.state.base import ConcurrentStateError, EntityNotFoundError, StateStore
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
        provisioner: RepositoryProvisioner | None = None,
        enforce_codex_account_policy: bool = False,
        account_policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
        usage_policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
        hard_cap_grace: timedelta = timedelta(seconds=120),
    ) -> None:
        if hard_cap_grace <= timedelta(0):
            raise ValueError("Hard-cap grace must be positive")
        self._store = store
        self._workspaces = workspace_manager
        self._drivers = drivers
        self._quota = quota_manager or QuotaManager(store)
        self._resources = resource_manager or ResourceManager(store)
        self._backends = backends
        self._provisioner = provisioner
        self._enforce_codex_account_policy = enforce_codex_account_policy
        self._account_policy = account_policy
        self._usage_policy = usage_policy
        self._hard_cap_grace = hard_cap_grace

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
                if not self._provider_allows_placement(job, placement):
                    continue
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
        log = event_logger(
            component="coordinator",
            operation="dispatch",
            job_id=job.id,
            node_id=placement.node_id,
            harness=placement.harness,
        )
        log.info("dispatch_started")

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

            if self._provisioner is not None:
                await self._provisioner.prepare(workspace)

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
            if isinstance(driver, ManagedHarnessDriver):
                handle = await driver.start_managed(run.id, contract)
            elif backend is not None:
                handle = await backend.dispatch(driver, contract)
            else:
                handle = await driver.start(contract)
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
            log.bind(run_id=run.id).info("dispatch_succeeded")
            return run
        except BaseException as error:
            log.bind(error_type=type(error).__name__).error("dispatch_failed")
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
            if cleanup_errors:
                log.bind(cleanup_error_count=len(cleanup_errors)).error(
                    "dispatch_compensation_incomplete"
                )
            elif quiesced:
                log.info("dispatch_compensated")
            raise

    def apply_provider_snapshot(self, snapshot: ProviderQuotaSnapshot) -> None:
        """Project provider telemetry into scheduling mode without inventing quota."""

        try:
            pool = self._store.get_quota_pool(snapshot.pool_id)
        except LookupError:
            return
        for _attempt in range(8):
            updated = replace(
                pool,
                mode=quota_mode_for_snapshot(
                    snapshot,
                    policy=self._account_policy,
                ),
                reset_at=snapshot.reset_at,
                reset_confidence=snapshot.confidence,
                updated_at=utc_now(),
            )
            try:
                self._store.update_quota_pool(pool, updated)
            except ConcurrentStateError:
                pool = self._store.get_quota_pool(snapshot.pool_id)
                continue
            return
        raise ConcurrentStateError(
            f"Could not apply provider snapshot for pool {snapshot.pool_id}"
        )

    async def recover_managed_runs(self) -> None:
        """Resume durable SDK threads after the service transport restarts."""

        first_error: BaseException | None = None
        for run in self._store.list_runs():
            if run.state not in {
                RunState.STARTING,
                RunState.RUNNING,
                RunState.DRAINING,
                RunState.CHECKPOINTED,
            }:
                continue
            job = self._store.get_job(run.job_id)
            if job.state not in {
                JobState.ADMITTED,
                JobState.RUNNING,
                JobState.DRAINING,
                JobState.CHECKPOINTED,
            }:
                continue
            driver = self._drivers.get(run.driver)
            if not isinstance(driver, ManagedHarnessDriver):
                continue
            log = event_logger(
                component="coordinator",
                operation="recovery",
                job_id=job.id,
                run_id=run.id,
                driver=run.driver,
            )
            repair_request = self._pending_repair(run.id)
            if repair_request is not None:
                try:
                    session = self._store.get_driver_session(run.id)
                    if session.active:
                        handle = await driver.recover(
                            run.id,
                            run.contract,
                            "Resume the interrupted repair turn from the durable "
                            "thread and existing workspace.",
                        )
                    else:
                        if not isinstance(driver, ContinuingManagedHarnessDriver):
                            raise LifecycleError(
                                f"Driver {run.driver} cannot continue repair turns"
                            )
                        instruction = repair_request.payload.get("instruction")
                        if not isinstance(instruction, str):
                            raise LifecycleError("Repair instruction is malformed")
                        handle = await driver.continue_turn(run.id, instruction)
                    recovered = replace(
                        run,
                        handle=handle,
                        state=RunState.RUNNING,
                        ended_at=None,
                        result=None,
                    )
                    self._store.save_run(recovered)
                    self._acknowledge_repair(repair_request, recovered)
                    log.info("repair_recovered")
                    continue
                except BaseException as error:
                    log.bind(error_type=type(error).__name__).error(
                        "repair_recovery_failed"
                    )
                    if first_error is None:
                        first_error = error
                    continue
            observation = driver.observe(run.id)
            if observation is not None and observation.terminal:
                continue
            try:
                await driver.recover(
                    run.id,
                    run.contract,
                    "The control-plane transport restarted. Resume the durable "
                    "thread from the existing workspace and return a structured "
                    "review handoff at the next safe boundary.",
                )
                log.info("managed_run_recovered")
            except (EntityNotFoundError, LookupError) as error:
                try:
                    self._abandon_unstarted_intent(job, run)
                except BaseException as cleanup_error:
                    error.add_note(f"Intent cleanup also failed: {cleanup_error}")
                    if first_error is None:
                        first_error = error
            except BaseException as error:
                log.bind(error_type=type(error).__name__).error(
                    "managed_run_recovery_failed"
                )
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    async def reconcile_managed_runs(
        self,
        snapshot: ProviderQuotaSnapshot | None = None,
        *,
        at: datetime | None = None,
    ) -> tuple[Job, ...]:
        """Apply streamed observations, policy commands, and terminal handoffs."""

        now = at or utc_now()
        finalized: list[Job] = []
        first_error: BaseException | None = None
        try:
            await self._start_pending_repairs()
        except BaseException as error:
            first_error = error
        for run in self._store.list_runs():
            job = self._store.get_job(run.job_id)
            if job.state not in {
                JobState.RUNNING,
                JobState.DRAINING,
                JobState.CHECKPOINTED,
            }:
                continue
            active_run = self._store.find_active_run(job.id)
            if active_run is None or active_run.id != run.id:
                continue
            driver = self._drivers.get(run.driver)
            if not isinstance(driver, ManagedHarnessDriver):
                continue
            observation = driver.observe(run.id)
            if observation is None:
                continue
            try:
                if observation.terminal:
                    finalized_job = self._finalize_managed_observation(run, observation)
                    finalized.append(finalized_job)
                    event_logger(
                        component="coordinator",
                        operation="reconcile",
                        job_id=job.id,
                        run_id=run.id,
                        outcome=(
                            observation.result.outcome.value
                            if observation.result is not None
                            else "unknown"
                        ),
                    ).info("managed_run_finalized")
                    continue
                active_snapshot = snapshot
                if active_snapshot is None:
                    reservation = self._store.get_reservation(run.reservation_id)
                    active_snapshot = self._store.latest_provider_quota_snapshot(
                        reservation.pool_id
                    )
                elif (
                    active_snapshot.pool_id
                    != self._store.get_reservation(run.reservation_id).pool_id
                ):
                    active_snapshot = self._store.latest_provider_quota_snapshot(
                        self._store.get_reservation(run.reservation_id).pool_id
                    )
                self._enqueue_usage_policy(
                    job,
                    run,
                    active_snapshot,
                    at=now,
                )
                if isinstance(driver, PendingCommandHarnessDriver):
                    await driver.process_pending(run.id)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
        return tuple(finalized)

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

    def promote_suspended_to_review(self, job_id: str) -> Job:
        """Promote a completed, quiescent checkpoint after operator validation."""

        job = self._store.get_job(job_id)
        if job.state is not JobState.SUSPENDED:
            raise LifecycleError(f"Job {job_id} is not suspended")
        run = self._require_latest_run(job_id)
        if run.state is not RunState.SUSPENDED or run.result is None:
            raise LifecycleError(f"Job {job_id} has no durable suspended result")
        if run.result.outcome is not RunOutcome.COMPLETED:
            raise LifecycleError(f"Job {job_id} did not report completed work")
        if self._store.find_active_allocation(job_id) is not None:
            raise LifecycleError(f"Job {job_id} still has an active allocation")
        reservation = self._store.find_active_reservation(job_id)
        if reservation is not None:
            raise LifecycleError(f"Job {job_id} still has an active reservation")

        result = self._trusted_workspace_commit_result(run, run.result)
        final_run = replace(run, result=result)
        review, event = transition_job(
            job,
            JobState.REVIEW,
            "operator promoted completed checkpoint after independent validation",
        )
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
        managed_codex = job.selected_harness == "codex"
        checkpoint_expectations = (
            "Return a compact resume capsule at safe boundaries and before "
            "preemption. Agentd records trusted Git handoffs outside the model "
            "sandbox."
            if managed_codex
            else (
                "Return a compact resume capsule at safe boundaries and before "
                "preemption. Commit durable handoffs."
            )
        )
        completion_protocol = (
            "Satisfy the acceptance criteria and run validation. Do not run Git "
            "commit, merge, or push commands; after valid terminal telemetry, "
            "agentd creates the trusted automation commit for explicit review."
            if managed_codex
            else (
                "Satisfy the acceptance criteria, run validation, commit changes, "
                "and report completion to the control plane. Do not merge."
            )
        )
        return ExecutionContract(
            job_id=job.id,
            objective=job.objective,
            scope=f"Project {job.project} in repository {job.repository}",
            acceptance_criteria=job.acceptance_criteria,
            dependency_results=dependency_results,
            role="implementation worker",
            allowed_filesystem_scope=(workspace.working_directory,),
            checkpoint_expectations=checkpoint_expectations,
            coordination_mechanisms=(
                "get_assignment",
                "request_refinement",
                "report_blocker",
                "checkpoint",
                "request_review",
                "complete",
            ),
            completion_protocol=completion_protocol,
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
        return job.base_ref

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

    def _provider_allows_placement(self, job: Job, placement: Placement) -> bool:
        if placement.harness != "codex" or not self._enforce_codex_account_policy:
            return True
        snapshot = self._store.latest_provider_quota_snapshot(job.quota_budget.pool_id)
        if snapshot is None:
            return job.qos in {QoSClass.INTERACTIVE, QoSClass.BLOCKER}
        return provider_allows_qos(
            snapshot,
            job.qos,
            policy=self._account_policy,
        )

    async def _start_pending_repairs(self) -> None:
        for job in self._store.list_jobs(frozenset({JobState.REVIEW})):
            run = self._store.latest_run(job.id)
            if run is None:
                continue
            request = self._pending_repair(run.id)
            if request is not None:
                try:
                    await self._start_repair(job, run, request)
                except QuotaAdmissionError:
                    continue

    async def _start_repair(
        self,
        job: Job,
        run: RunRecord,
        request: RunCommand,
    ) -> RunRecord:
        instruction = request.payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise LifecycleError("Repair instruction is malformed")
        driver = self._drivers.get(run.driver)
        if not isinstance(driver, ManagedHarnessDriver):
            raise LifecycleError(f"Driver {run.driver} cannot run durable repairs")
        if not isinstance(driver, ContinuingManagedHarnessDriver):
            raise LifecycleError(f"Driver {run.driver} cannot continue repair turns")
        session = self._store.get_driver_session(run.id)
        raw_count = session.metadata.get("continuation_count", 0)
        continuation_count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool)
            else 0
        )
        if continuation_count >= 2:
            raise LifecycleError("The two-repair-turn limit has been reached")

        capabilities = {item.name: item for item in self._drivers.capabilities()}
        placements = [
            placement
            for placement in compatible_placements(
                job,
                self._store.list_nodes(),
                capabilities,
            )
            if placement.harness == run.driver
            and self._provider_allows_placement(job, placement)
        ]
        if not placements:
            raise QuotaAdmissionError(
                "No policy-compliant capacity is available for repair"
            )
        placement = placements[0]

        maximum = job.quota_budget.maximum
        if maximum is None:
            raise LifecycleError("A Codex repair requires a cumulative maximum")
        consumed = sum(item.consumed for item in self._store.list_reservations(job.id))
        remaining = maximum - consumed
        if remaining <= 0:
            raise LifecycleError("The cumulative job maximum has been reached")
        expected = min(job.quota_budget.expected_path, remaining)
        repair_budget = replace(
            job.quota_budget,
            implementation=expected,
            review=0,
            repair=0,
            validation=0,
        )
        reservation = self._quota.reserve(replace(job, quota_budget=repair_budget))
        log = event_logger(
            component="coordinator",
            operation="repair",
            job_id=job.id,
            run_id=run.id,
            repair_turn=continuation_count + 1,
        )
        log.info("repair_started")
        allocation = None
        handle: RunHandle | None = None
        running: Job | None = None
        try:
            node = self._store.get_node(placement.node_id)
            allocation = self._resources.allocate(job, node)
            workspace = self._store.get_workspace(run.workspace_id)
            if (
                workspace.state is not WorkspaceState.LEASED
                or not self._workspaces.is_available(workspace)
            ):
                raise LifecycleError("The reviewed workspace is unavailable for repair")
            if self._provisioner is not None:
                await self._provisioner.prepare(workspace)

            if run.result is not None:
                capsule = self._capsule_from_result(run.result)
                latest = self._store.latest_checkpoint(job.id)
                if (
                    latest is None
                    or latest.run_id != run.id
                    or latest.capsule != capsule
                ):
                    self._store.save_checkpoint(
                        Checkpoint(job_id=job.id, run_id=run.id, capsule=capsule)
                    )
            contract = self._build_contract(job, workspace)
            starting = replace(
                run,
                node_id=placement.node_id,
                reservation_id=reservation.id,
                allocation_id=allocation.id,
                contract=contract,
                handle=RunHandle(id=f"pending-{new_id()}", driver=run.driver),
                state=RunState.STARTING,
                started_at=utc_now(),
                ended_at=None,
                result=None,
            )
            running, event = transition_job(
                job,
                JobState.RUNNING,
                f"bounded repair turn {continuation_count + 1} requested",
            )
            self._store.save_job_and_run(running, event, starting)
            handle = await driver.continue_turn(run.id, instruction.strip())
            active = replace(starting, handle=handle, state=RunState.RUNNING)
            self._store.save_run(active)
            self._acknowledge_repair(request, active)
            return active
        except BaseException:
            quiesced = handle is None
            if handle is not None:
                try:
                    await driver.cancel(handle)
                    await driver.collect(handle)
                    quiesced = True
                except BaseException:
                    quiesced = False
            if quiesced:
                if allocation is not None:
                    self._resources.release(allocation.id)
                self._quota.release(reservation.id, cancelled=True)
                if running is not None:
                    review, event = transition_job(
                        running,
                        JobState.REVIEW,
                        "repair start failed; reviewed handoff restored",
                    )
                    self._store.save_job_and_run(review, event, run)
            raise

    def _pending_repair(self, run_id: str) -> RunCommand | None:
        return next(
            (
                command
                for command in self._store.list_pending_run_commands(run_id)
                if command.action == "repair"
            ),
            None,
        )

    def _acknowledge_repair(
        self,
        request: RunCommand,
        run: RunRecord,
    ) -> None:
        observation = self._drivers.get(run.driver).observe(run.id)
        self._store.acknowledge_run_command(
            RunCommandAck(
                command_id=request.id,
                run_id=run.id,
                detail="repair turn started on the durable Codex thread",
                observation_cursor=(
                    observation.cursor if observation is not None else None
                ),
            )
        )

    def _enqueue_usage_policy(
        self,
        job: Job,
        run: RunRecord,
        snapshot: ProviderQuotaSnapshot | None,
        *,
        at: datetime,
    ) -> None:
        reservation = self._store.get_reservation(run.reservation_id)
        consumed = sum(item.consumed for item in self._store.list_reservations(job.id))
        used_percent = provider_used_percent(snapshot) if snapshot is not None else None
        provider_has_capacity = (
            snapshot is None and not self._enforce_codex_account_policy
        ) or (
            snapshot is not None
            and not provider_quota_reached(snapshot)
            and (
                not snapshot_is_stale(
                    snapshot,
                    at=at,
                    policy=self._account_policy,
                )
                or job.qos in {QoSClass.INTERACTIVE, QoSClass.BLOCKER}
            )
            and (
                used_percent is None
                or used_percent < self._account_policy.urgent_only_used_percent
                or job.qos in {QoSClass.INTERACTIVE, QoSClass.BLOCKER}
            )
        )
        if should_top_up(reservation, self._usage_policy) and provider_has_capacity:
            prior_consumed = consumed - reservation.consumed
            maximum_for_attempt = (
                job.quota_budget.maximum - prior_consumed
                if job.quota_budget.maximum is not None
                else reservation.amount + self._usage_policy.top_up_chunk
            )
            top_up = min(
                self._usage_policy.top_up_chunk,
                max(0, maximum_for_attempt - reservation.amount),
            )
            if top_up > 0:
                # A local capacity race is an admission signal, not a reason
                # to lose the already-running turn or its telemetry.
                with suppress(ConcurrentStateError):
                    self._quota.top_up(reservation.id, top_up)

        maximum_command = maximum_checkpoint_command(
            job_id=job.id,
            run_id=run.id,
            consumed=consumed,
            maximum=job.quota_budget.maximum,
            at=at,
            policy=self._usage_policy,
        )
        if maximum_command is not None and not self._job_has_command(
            job.id,
            maximum_command.id,
        ):
            self._store.enqueue_run_command(maximum_command)

        if snapshot is not None:
            provider_command = provider_checkpoint_command(
                snapshot,
                run_id=run.id,
                qos=job.qos,
                at=at,
                policy=self._account_policy,
            )
            if provider_command is not None:
                self._store.enqueue_run_command(provider_command)

        reached_at = self._first_hard_cap_at(job)
        if reached_at is not None and at >= reached_at + self._hard_cap_grace:
            interrupt = hard_cap_interrupt_command(
                job_id=job.id,
                run_id=run.id,
                consumed=consumed,
                maximum=job.quota_budget.maximum,
                reached_at=reached_at,
                grace=self._hard_cap_grace,
                policy=self._usage_policy,
            )
            if interrupt is not None:
                self._store.enqueue_run_command(interrupt)

    def _first_hard_cap_at(self, job: Job) -> datetime | None:
        maximum = job.quota_budget.maximum
        if maximum is None:
            return None
        samples = sorted(
            (
                sample
                for run in self._store.list_runs(job.id)
                for sample in self._store.list_usage_samples(run.id)
            ),
            key=lambda sample: (sample.observed_at, sample.id),
        )
        cumulative = 0.0
        for sample in samples:
            cumulative += sample.delta or 0
            if cumulative >= maximum:
                return sample.observed_at
        return None

    def _finalize_managed_observation(
        self,
        run: RunRecord,
        observation: RunObservation,
    ) -> Job:
        job = self._store.get_job(run.job_id)
        run = self._store.get_run(run.id)
        if job.state not in {
            JobState.RUNNING,
            JobState.DRAINING,
            JobState.CHECKPOINTED,
        }:
            return job
        if observation.run_id != run.id:
            raise LifecycleError("Managed observation belongs to another run")
        result = observation.result
        if result is None:
            result = RunResult(
                outcome=RunOutcome.FAILED,
                summary="Codex ended without a terminal result",
                metadata={"telemetry_valid": False},
            )
        result = self._trusted_workspace_result(run, result)
        self._store.save_run(replace(run, result=result))

        allocation = self._store.find_active_allocation(job.id)
        if allocation is not None:
            self._resources.release(allocation.id)

        observed_cumulative = observation.normalized_cumulative_quota
        matching_samples = [
            sample
            for sample in self._store.list_usage_samples(run.id)
            if sample.thread_id == observation.thread_id
            and sample.turn_id == observation.turn_id
        ]
        ledger_valid = observed_cumulative is not None and any(
            sample.cumulative_quota == observed_cumulative
            for sample in matching_samples
        )
        telemetry_valid = (
            observation.telemetry_valid
            and observation.usage is not None
            and ledger_valid
        )
        if not telemetry_valid:
            final_run = replace(
                run,
                state=RunState.SUSPENDED,
                ended_at=utc_now(),
                result=result,
            )
            self._store.save_run(final_run)
            self._quota.begin_metering(
                job.id,
                reason=(
                    "Codex turn ended without valid terminal token telemetry; "
                    "acceptance blocked pending reconciliation"
                ),
            )
            return self._store.get_job(job.id)

        if result.outcome is RunOutcome.COMPLETED:
            result = self._trusted_workspace_commit_result(run, result)
            run = replace(run, result=result)
            # The Git ref is an external effect. Persist its trusted value while
            # terminal reconciliation remains retryable, before entering REVIEW.
            self._store.save_run(run)

        reservation = self._store.get_reservation(run.reservation_id)
        self._quota.release(
            reservation.id,
            consumed=reservation.consumed,
            cancelled=result.outcome is RunOutcome.CANCELLED,
        )
        commands = self._store.list_run_commands(run.id)
        job_consumed = sum(
            item.consumed for item in self._store.list_reservations(job.id)
        )
        suspension_requested = any(
            command.action in {"checkpoint", "suspend"} for command in commands
        ) or should_checkpoint_for_maximum(
            job_consumed,
            job.quota_budget.maximum,
            self._usage_policy,
        )
        if suspension_requested:
            return self._finalize_managed_suspension(job, run, result)

        if result.outcome is RunOutcome.COMPLETED:
            review, event = transition_job(
                job,
                JobState.REVIEW,
                f"Codex turn {observation.turn_id} is ready for explicit review",
            )
            final_run = replace(
                run,
                state=RunState.SUSPENDED,
                ended_at=utc_now(),
                result=result,
            )
            self._store.save_job_and_run(review, event, final_run)
            return review

        target = (
            JobState.CANCELLED
            if result.outcome is RunOutcome.CANCELLED
            else JobState.FAILED
        )
        final, event = transition_job(
            job,
            target,
            f"Codex turn {observation.turn_id} reported {result.outcome.value}",
        )
        final_run = replace(
            run,
            state=(
                RunState.CANCELLED
                if result.outcome is RunOutcome.CANCELLED
                else RunState.FAILED
            ),
            ended_at=utc_now(),
            result=result,
        )
        self._store.save_job_and_run(final, event, final_run)
        return final

    def _finalize_managed_suspension(
        self,
        job: Job,
        run: RunRecord,
        result: RunResult,
    ) -> Job:
        capsule = self._capsule_from_result(result)
        latest = self._store.latest_checkpoint(job.id)
        if latest is None or latest.run_id != run.id or latest.capsule != capsule:
            self._store.save_checkpoint(
                Checkpoint(job_id=job.id, run_id=run.id, capsule=capsule)
            )

        if job.state is JobState.RUNNING:
            job, event = transition_job(
                job,
                JobState.DRAINING,
                "Codex stopped at a control-plane safe boundary",
            )
            run = replace(run, state=RunState.DRAINING, result=result)
            self._store.save_job_and_run(job, event, run)
        if job.state is JobState.DRAINING:
            job, event = transition_job(
                job,
                JobState.CHECKPOINTED,
                "structured Codex checkpoint capsule persisted",
            )
            run = replace(run, state=RunState.CHECKPOINTED, result=result)
            self._store.save_job_and_run(job, event, run)
        suspended, event = transition_job(
            job,
            JobState.SUSPENDED,
            "checkpoint complete; workspace retained and execution capacity released",
        )
        final_run = replace(
            run,
            state=RunState.SUSPENDED,
            ended_at=utc_now(),
            result=result,
        )
        self._store.save_job_and_run(suspended, event, final_run)
        return suspended

    def _trusted_workspace_result(
        self,
        run: RunRecord,
        result: RunResult,
    ) -> RunResult:
        workspace = self._store.get_workspace(run.workspace_id)
        current = self._workspaces.current_commit(workspace)
        if result.commit is None or result.commit == current:
            return replace(result, commit=current)
        return replace(
            result,
            commit=current,
            metadata={
                **result.metadata,
                "untrusted_reported_commit": result.commit,
            },
        )

    def _trusted_workspace_commit_result(
        self,
        run: RunRecord,
        result: RunResult,
    ) -> RunResult:
        workspace = self._store.get_workspace(run.workspace_id)
        commit = self._workspaces.commit_changes(workspace)
        if workspace.commit != commit:
            self._store.save_workspace(replace(workspace, commit=commit))
        return replace(result, commit=commit)

    @staticmethod
    def _capsule_from_result(result: RunResult) -> ResumeCapsule:
        def values(name: str) -> tuple[str, ...]:
            raw = result.metadata.get(name)
            if not isinstance(raw, list):
                return ()
            return tuple(str(item) for item in raw)

        current = values("current") or ((result.summary,) if result.summary else ())
        return ResumeCapsule(
            completed=values("completed"),
            current=current,
            next_steps=values("next_steps"),
            commit=result.commit,
            known_failures=values("known_failures"),
            decisions=values("decisions"),
        )

    def _abandon_unstarted_intent(self, job: Job, run: RunRecord) -> None:
        result = RunResult(
            outcome=RunOutcome.CANCELLED,
            summary="managed driver session was never durably created",
        )
        cancelled_run = replace(
            run,
            state=RunState.CANCELLED,
            ended_at=utc_now(),
            result=result,
        )
        self._store.save_run(cancelled_run)
        self._raise_cleanup_errors(
            self._cleanup_job_resources(job.id, cancelled_run, completed=False)
        )
        target = JobState.READY if job.state is JobState.ADMITTED else JobState.FAILED
        updated, event = transition_job(
            job,
            target,
            "startup reconciliation found no durable managed-driver session",
        )
        self._store.save_job_and_run(updated, event, cancelled_run)

    def _job_has_command(self, job_id: str, command_id: str) -> bool:
        return any(
            command.id == command_id
            for run in self._store.list_runs(job_id)
            for command in self._store.list_run_commands(run.id)
        )

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
