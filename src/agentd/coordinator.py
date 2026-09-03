"""Effectful scheduler coordinator for the local MVP lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from inspect import isawaitable

from agentd.domain.enums import (
    ArtifactKind,
    JobState,
    PreemptionPolicy,
    QoSClass,
    QuotaMode,
    ReservationState,
    RunOutcome,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import (
    ArtifactInput,
    ArtifactRecord,
    ArtifactRef,
    BuildImageOperation,
    Checkpoint,
    DeployImageOperation,
    DriverSession,
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
    WorkerHeartbeat,
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
from agentd.runtime.governor import (
    DEFAULT_PROVIDER_STOP_POLICY,
    ProviderStopPolicy,
    provider_stop_command,
    tail_governor_command,
)
from agentd.runtime.quota import QuotaAdmissionError, QuotaManager
from agentd.runtime.resources import ResourceManager
from agentd.scheduling.burn import burn_order_key
from agentd.scheduling.placement import Placement, compatible_placements
from agentd.scheduling.readiness import gang_readiness
from agentd.state.base import ConcurrentStateError, EntityNotFoundError, StateStore
from agentd.workers.errors import WorkerStartUncertainError, WorkerTransportError
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE, WorkerBackend
from agentd.workers.registry import BackendRegistry
from agentd.workspaces.base import WorkspaceManager, WorkspaceReleaseError


class LifecycleError(RuntimeError):
    pass


class _ArtifactUnavailable(LifecycleError):
    """A valid job is waiting for an immutable input to be verified."""

    pass


_MANAGED_TELEMETRY_VALID = "agentd_terminal_telemetry_valid"
_MANAGED_FINALIZATION_TARGET = "agentd_finalization_target"
_MANAGED_FINALIZATION_TURN = "agentd_finalization_turn"


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
        provider_stop_policy: ProviderStopPolicy = DEFAULT_PROVIDER_STOP_POLICY,
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
        self._provider_stop_policy = provider_stop_policy
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
                resolved_inputs, resolved_operation = self._resolve_artifacts(job)
            except _ArtifactUnavailable:
                # Artifact readiness is an admission prerequisite.  Waiting for
                # a producer or operator-verified external input must not reserve
                # quota, allocate capacity, or create a workspace.
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
                backend = self._select_backend(placement.node_id, job)
                if self._backends is not None and backend is None:
                    continue
                try:
                    return await self._dispatch(
                        job,
                        placement,
                        backend,
                        resolved_inputs=resolved_inputs,
                        resolved_operation=resolved_operation,
                    )
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
        *,
        resolved_inputs: tuple[ArtifactRef, ...],
        resolved_operation: BuildImageOperation | DeployImageOperation | None,
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
            reservation = self._quota.reserve(self._job_for_next_reservation(job))
            node = self._store.get_node(placement.node_id)
            allocation = self._resources.allocate(job, node)
            allocation_id = allocation.id

            typed_operation = resolved_operation is not None
            workspace = self._store.find_workspace(job.id)
            if typed_operation:
                if workspace is None or workspace.state is not WorkspaceState.LEASED:
                    workspace = self._operation_workspace(
                        job,
                        placement,
                        backend,
                    )
                    self._store.save_workspace(workspace, expected=None)
                    workspace_created = True
            else:
                if (
                    workspace is not None
                    and workspace.state is WorkspaceState.LEASED
                    and not self._workspaces.is_available(workspace)
                ):
                    self._store.save_workspace(
                        replace(workspace, state=WorkspaceState.FAILED),
                        expected=workspace,
                    )
                    workspace = None
                if workspace is None or workspace.state is not WorkspaceState.LEASED:
                    workspace = self._workspaces.allocate(
                        job,
                        base_ref=self._base_ref(job),
                    )
                    self._store.save_workspace(workspace, expected=None)
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
            self._store.save_job(candidate, event, expected=job)
            admitted = candidate

            contract = self._build_contract(
                admitted,
                workspace,
                artifact_inputs=resolved_inputs,
                operation=resolved_operation,
            )
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
            self._store.save_run(run, expected=None)
            managed = isinstance(driver, ManagedHarnessDriver)
            if backend is not None:
                handle = await backend.dispatch(
                    driver,
                    contract,
                    run_id=run.id,
                    managed=managed,
                )
            elif managed:
                handle = await driver.start_managed(run.id, contract)
            else:
                handle = await driver.start(contract)
            if managed and backend is not None and backend.capabilities().remote:
                self._create_remote_driver_session(run, handle)
            # Persist the externally meaningful handle before publishing RUNNING.
            # A reconciler can now identify and stop a process even if the job/run
            # transition below is interrupted.
            updated_run = replace(run, handle=handle)
            self._store.save_run(updated_run, expected=run)
            run = updated_run
            running_run = replace(run, state=RunState.RUNNING)
            running, running_event = transition_job(
                admitted,
                JobState.RUNNING,
                f"harness run {run.id} started",
            )
            self._store.save_job_and_run(
                running,
                running_event,
                running_run,
                expected_job=admitted,
                expected_run=run,
            )
            run = running_run
            log.bind(run_id=run.id).info("dispatch_succeeded")
            return run
        except BaseException as error:
            log.bind(error_type=type(error).__name__).error("dispatch_failed")
            cleanup_errors: list[BaseException] = []
            uncertain_remote_start = (
                handle is None
                and run is not None
                and backend is not None
                and backend.capabilities().remote
                and (
                    isinstance(
                        error,
                        (WorkerStartUncertainError, asyncio.CancelledError),
                    )
                )
            )
            # A remote START can have created a process before its response
            # was lost.  The durable STARTING/ADMITTED intent, reservation,
            # and allocation are then the only safe ownership record.  Do not
            # cancel, release, or return the job to READY: recovery must query
            # this same run id before deciding anything.
            quiesced = handle is None and not uncertain_remote_start
            if handle is not None:
                try:
                    if backend is not None:
                        cleanup_target = (
                            run.id
                            if backend.capabilities().remote and run is not None
                            else handle
                        )
                        await backend.cancel(cleanup_target)
                        await backend.collect(cleanup_target)
                    else:
                        await driver.cancel(handle)
                        await driver.collect(handle)
                    quiesced = True
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if quiesced:
                if workspace_created and workspace is not None:
                    try:
                        if self._is_operation_workspace(workspace):
                            self._release_operation_workspace(workspace)
                        else:
                            self._release_workspace(
                                workspace,
                                retain_on_failure=True,
                            )
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
                    self._store.save_job_and_run(
                        ready,
                        event,
                        cancelled_run,
                        expected_job=admitted,
                        expected_run=run,
                    )
                else:
                    self._store.save_job(ready, event, expected=admitted)
            for cleanup_error in cleanup_errors:
                error.add_note(f"Cleanup also failed: {cleanup_error}")
            if cleanup_errors:
                log.bind(cleanup_error_count=len(cleanup_errors)).error(
                    "dispatch_compensation_incomplete"
                )
            elif quiesced:
                log.info("dispatch_compensated")
            elif uncertain_remote_start:
                log.warning(
                    "remote_start_outcome_uncertain_intent_retained",
                    run_id=run.id if run is not None else None,
                )
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

    async def refresh_worker_heartbeats(self) -> tuple[dict[str, object], ...]:
        """Refresh authenticated remote backends and persist node liveness.

        Capacity remains an operator-owned scheduling declaration on
        :class:`WorkerNode`; a heartbeat never fabricates or silently expands
        it.  The authenticated worker identity is instead used to bind the
        configured backend to that existing node and advance ``updated_at``.
        """

        if self._backends is None:
            return ()
        snapshots: list[dict[str, object]] = []
        errors: list[Exception] = []
        for backend_name in self._backends.names():
            backend = self._backends.get(backend_name)
            if not backend.capabilities().remote:
                continue
            try:
                raw_snapshot = await backend.heartbeat()
                snapshot = dict(raw_snapshot)
                node_id = snapshot.get("node_id")
                if not isinstance(node_id, str) or not node_id.strip():
                    raise LifecycleError(
                        f"Remote backend {backend_name!r} returned no node identity"
                    )
                node = self._store.get_node(node_id)
                session_epoch = snapshot.get("session_epoch")
                drivers = snapshot.get("drivers")
                active_runs = snapshot.get("active_runs")
                if (
                    not isinstance(session_epoch, str)
                    or not session_epoch.strip()
                    or not isinstance(drivers, list)
                    or any(not isinstance(driver, str) for driver in drivers)
                    or isinstance(active_runs, bool)
                    or not isinstance(active_runs, int)
                    or active_runs < 0
                ):
                    raise LifecycleError(
                        f"Remote backend {backend_name!r} returned invalid status"
                    )
                declared_backend = node.labels.get("backend")
                if declared_backend not in {None, backend_name}:
                    raise LifecycleError(
                        f"Node {node_id!r} is bound to backend "
                        f"{declared_backend!r}, not {backend_name!r}"
                    )
                refreshed = replace(
                    node,
                    labels={**node.labels, "backend": backend_name},
                    updated_at=utc_now(),
                    heartbeat=WorkerHeartbeat(
                        session_epoch=session_epoch,
                        drivers=frozenset(drivers),
                        active_runs=active_runs,
                    ),
                )
                self._store.register_node(refreshed)
                snapshots.append(snapshot)
            except Exception as error:
                errors.append(error)
                event_logger(
                    component="coordinator",
                    operation="worker_heartbeat",
                    backend=backend_name,
                    error_type=type(error).__name__,
                ).error("worker_heartbeat_failed")
        if errors:
            raise ExceptionGroup("one or more remote worker heartbeats failed", errors)
        return tuple(snapshots)

    async def recover_managed_runs(self) -> None:
        """Recover durable SDK threads and typed-operation startup intents."""

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
            # A START response can be lost after the remote worker has
            # claimed the durable run.  Reconcile that persisted intent before
            # entering any driver recovery path; it must never dispatch a new
            # request or use a controller-local fallback.
            try:
                backend = self._backend_for_run(run)
            except BaseException as error:
                if first_error is None:
                    first_error = error
                continue
            if job.state is JobState.ADMITTED and (
                backend is not None and backend.capabilities().remote
            ):
                try:
                    await self._recover_remote_starting_intent(job, run, backend)
                except BaseException as error:
                    event_logger(
                        component="coordinator",
                        operation="remote_start_recovery",
                        job_id=job.id,
                        run_id=run.id,
                        error_type=type(error).__name__,
                    ).error("remote_start_recovery_failed")
                    if first_error is None:
                        first_error = error
                continue
            if self._is_typed_operation_run(run, driver):
                if job.state is JobState.ADMITTED:
                    log = event_logger(
                        component="coordinator",
                        operation="typed_operation_recovery",
                        job_id=job.id,
                        run_id=run.id,
                        driver=run.driver,
                    )
                    try:
                        # START was already durably addressed by ``run.id``.
                        # Query that identity without dispatching again, then
                        # publish RUNNING so the normal idempotent terminal
                        # path can either continue or fail an unknown run.
                        status = await self._operation_status(run)
                        recovered = replace(run, state=RunState.RUNNING)
                        running, event = transition_job(
                            job,
                            JobState.RUNNING,
                            f"recovered typed operation intent {run.id}",
                        )
                        self._store.save_job_and_run(
                            running,
                            event,
                            recovered,
                            expected_job=job,
                            expected_run=run,
                        )
                        await self._reconcile_operation_run(
                            recovered,
                            status=status,
                        )
                        log.info("typed_operation_recovered")
                    except BaseException as error:
                        log.bind(error_type=type(error).__name__).error(
                            "typed_operation_recovery_failed"
                        )
                        if first_error is None:
                            first_error = error
                # Already-published typed runs are reconciled at the start of
                # the daemon's first normal tick. They must never enter the SDK
                # recovery path or re-run START.
                continue
            if not isinstance(driver, ManagedHarnessDriver):
                continue
            log = event_logger(
                component="coordinator",
                operation="recovery",
                job_id=job.id,
                run_id=run.id,
                driver=run.driver,
            )
            try:
                await self._prepare_recovery_workspace(job, run)
            except BaseException as error:
                log.bind(error_type=type(error).__name__).error(
                    "managed_run_workspace_revalidation_failed"
                )
                if first_error is None:
                    first_error = error
                continue
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
                    self._store.save_run(recovered, expected=run)
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
            backend = self._backend_for_run(run)
            if backend is not None and backend.capabilities().remote:
                try:
                    observation = await backend.observe(run.id)
                    if observation is not None:
                        self._record_remote_observation(run, observation)
                        continue
                    raise LifecycleError(
                        f"Remote managed run {run.id} has no recoverable observation"
                    )
                except BaseException as error:
                    log.bind(error_type=type(error).__name__).error(
                        "remote_managed_run_recovery_failed"
                    )
                    if first_error is None:
                        first_error = error
                    continue
            observation = driver.observe(run.id)
            if (
                observation is not None
                and observation.terminal
                and run.state is not RunState.STARTING
                and job.state is not JobState.ADMITTED
            ):
                continue
            try:
                handle = await driver.recover(
                    run.id,
                    run.contract,
                    "The control-plane transport restarted. Resume the durable "
                    "thread from the existing workspace and return a structured "
                    "review handoff at the next safe boundary.",
                )
                recovered = replace(
                    run,
                    handle=handle,
                    state=RunState.RUNNING,
                    ended_at=None,
                )
                if job.state is JobState.ADMITTED:
                    running, event = transition_job(
                        job,
                        JobState.RUNNING,
                        f"recovered managed run {run.id} published",
                    )
                    self._store.save_job_and_run(
                        running,
                        event,
                        recovered,
                        expected_job=job,
                        expected_run=run,
                    )
                else:
                    self._store.save_run(recovered, expected=run)
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

    async def _recover_remote_starting_intents(self) -> None:
        """Reconcile remote ADMITTED/STARTING intents during normal ticks."""

        first_error: BaseException | None = None
        for run in self._store.list_runs():
            if run.state is not RunState.STARTING:
                continue
            job = self._store.get_job(run.job_id)
            if job.state is not JobState.ADMITTED:
                continue
            try:
                backend = self._backend_for_run(run)
            except BaseException as error:
                if first_error is None:
                    first_error = error
                continue
            if backend is None or not backend.capabilities().remote:
                continue
            try:
                await self._recover_remote_starting_intent(job, run, backend)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    async def _recover_remote_starting_intent(
        self,
        job: Job,
        run: RunRecord,
        backend: WorkerBackend,
    ) -> None:
        """Resolve one remote START uncertainty by durable run id only."""

        status = await backend.status(run.id)
        if not isinstance(status, dict):
            raise LifecycleError(f"Remote run {run.id} returned malformed status")
        known = status.get("known")
        terminal = status.get("terminal")
        raw_result = status.get("result")
        if (
            not isinstance(known, bool)
            or not isinstance(terminal, bool)
            or (not known and (terminal or raw_result is not None))
            or (not terminal and raw_result is not None)
        ):
            raise LifecycleError(f"Remote run {run.id} returned invalid status")
        if not known:
            # This worker-authenticated answer is the only point at which a
            # lost START may be failed and its admission effects released.
            result = RunResult(
                RunOutcome.FAILED,
                "worker lost the remote run state after START uncertainty",
                metadata={"worker_status": "unknown"},
            )
            final_run = replace(
                run,
                state=RunState.FAILED,
                ended_at=utc_now(),
                result=result,
            )
            failed, event = transition_job(
                job,
                JobState.FAILED,
                f"remote run {run.id} is no longer known by the worker",
            )
            self._store.save_job_and_run(
                failed,
                event,
                final_run,
                expected_job=job,
                expected_run=run,
            )
            self._raise_cleanup_errors(
                self._cleanup_job_resources(job.id, final_run, completed=False)
            )
            return

        recovered = replace(run, state=RunState.RUNNING)
        running, event = transition_job(
            job,
            JobState.RUNNING,
            f"recovered remote START intent {run.id}",
        )
        self._store.save_job_and_run(
            running,
            event,
            recovered,
            expected_job=job,
            expected_run=run,
        )
        if self._is_typed_operation_run(run, self._drivers.get(run.driver)):
            await self._reconcile_operation_run(recovered, status=status)
            return
        if not terminal:
            return
        if raw_result is None:
            result = await backend.collect(run.id)
        else:
            try:
                result = RunResult.from_dict(raw_result)
            except (KeyError, TypeError, ValueError) as error:
                raise LifecycleError(
                    f"Remote run {run.id} returned malformed terminal result"
                ) from error
        self._store.save_run(
            replace(recovered, result=result),
            expected=recovered,
        )
        await self.complete(job.id)

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
        # Do not retry cleanup for a job that this same pass just made
        # terminal.  Keeping the retry on the next tick makes a one-shot
        # release failure observable while preserving the durable terminal
        # result and the admission for retry.
        terminal_cleanup_candidates = frozenset(
            job.id
            for job in self._store.list_jobs(
                frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})
            )
        )
        try:
            # This also runs after an in-process dispatch returned an
            # uncertain START, not only during daemon bootstrap.
            await self._recover_remote_starting_intents()
        except BaseException as error:
            first_error = error
        try:
            # Terminal persistence and resource cleanup are separate durable
            # effects. A transient release failure must remain retryable on a
            # later normal tick instead of stranding a terminal job forever.
            self._retry_terminal_resource_cleanups(terminal_cleanup_candidates)
        except BaseException as error:
            if first_error is None:
                first_error = error
        for job in self._store.list_jobs(frozenset({JobState.METERING_PENDING})):
            run = self._store.latest_run(job.id)
            if run is None or run.result is None:
                continue
            marker = run.result.metadata.get(_MANAGED_TELEMETRY_VALID)
            if not isinstance(marker, bool):
                continue
            driver = self._drivers.get(run.driver)
            if not isinstance(driver, ManagedHarnessDriver):
                continue
            try:
                recovered = self._resume_managed_finalization(job, run)
                if recovered.state is not JobState.METERING_PENDING:
                    finalized.append(recovered)
            except BaseException as error:
                if first_error is None:
                    first_error = error
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
            if self._is_typed_operation_run(run, driver):
                try:
                    finalized_job = await self._reconcile_operation_run(run)
                    if finalized_job is not None:
                        finalized.append(finalized_job)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                continue
            if not isinstance(driver, ManagedHarnessDriver):
                continue
            observation = await self._observe_run(run)
            if observation is None:
                continue
            try:
                if self._is_remote_run(run):
                    self._record_remote_observation(run, observation)
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
                    observation=observation,
                )
                if self._is_remote_run(run):
                    await self._deliver_remote_commands(run, observation)
                elif isinstance(driver, PendingCommandHarnessDriver):
                    await driver.process_pending(run.id)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
        return tuple(finalized)

    def _retry_terminal_resource_cleanups(
        self,
        job_ids: frozenset[str],
    ) -> None:
        """Retry idempotent workspace/capacity cleanup for terminal jobs."""

        first_error: BaseException | None = None
        terminal_states = frozenset(
            {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
        )
        for job in self._store.list_jobs(terminal_states):
            if job.id not in job_ids:
                continue
            run = self._store.latest_run(job.id)
            try:
                self._raise_cleanup_errors(
                    self._cleanup_job_resources(
                        job.id,
                        run,
                        completed=job.state is JobState.COMPLETED,
                        cancelled=job.state is JobState.CANCELLED,
                    )
                )
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    @staticmethod
    def _is_typed_operation_run(
        run: RunRecord,
        driver: object,
    ) -> bool:
        """Identify runs owned by the typed build/deploy harness.

        A contract may carry an operation while a legacy managed harness is
        still being used for its normal observation/command lifecycle.  Only a
        driver advertising the matching typed-operation feature is eligible
        for status-based finalization; this keeps that compatibility path from
        being mistaken for a worker operation run.
        """

        operation = run.contract.operation
        if operation is None:
            return False
        capabilities = getattr(driver, "capabilities", None)
        if not callable(capabilities):
            return False
        features = getattr(capabilities(), "features", frozenset())
        if isinstance(operation, BuildImageOperation):
            return "build-image" in features
        if isinstance(operation, DeployImageOperation):
            return "deploy-image" in features
        return False

    async def _operation_status(self, run: RunRecord) -> dict[str, object]:
        """Read status for a typed operation using its durable run identity."""

        backend = self._backend_for_run(run)
        if backend is None:
            if run.backend != "direct":
                raise LifecycleError(
                    f"Run {run.id} references unavailable remote backend"
                )
            driver = self._drivers.get(run.driver)
            status_method = getattr(driver, "status", None)
            if status_method is None:
                raise LifecycleError(f"Typed run {run.id} has no local status provider")
            status = status_method(run.handle)
            if isawaitable(status):
                status = await status
        else:
            # The durable control-plane run id is the worker's primary key. A
            # freshly restarted controller has no in-memory handle mapping.
            status_target = run.id if backend.capabilities().remote else run.handle
            status = await backend.status(status_target)
        if not isinstance(status, dict):
            raise LifecycleError(f"Typed run {run.id} returned malformed status")
        return status

    async def _reconcile_operation_run(
        self,
        run: RunRecord,
        *,
        status: dict[str, object] | None = None,
    ) -> Job | None:
        """Collect one typed operation only after a terminal worker status.

        Status is a read-only, authenticated worker query.  A worker that no
        longer knows the run is treated as a failed attempt, which releases
        admission resources through the same atomic completion path as a
        normal failed operation.  Transport/backend failures remain retryable
        and never fall back to a controller-local driver.
        """

        # Persisting the worker result is deliberately a separate step from
        # publishing the terminal run/job transition.  If the controller dies
        # in that window, the durable result is authoritative and no second
        # worker status query (which could now report ``known=False``) is
        # needed to finish the same idempotent completion path.
        if run.result is not None:
            return await self.complete(run.job_id)

        backend = self._backend_for_run(run)
        if status is None:
            status = await self._operation_status(run)
        known = status.get("known")
        terminal = status.get("terminal")
        raw_result = status.get("result")
        if not isinstance(known, bool) or not isinstance(terminal, bool):
            raise LifecycleError(f"Typed run {run.id} returned malformed status flags")
        if not known:
            if terminal or raw_result is not None:
                raise LifecycleError(
                    f"Typed run {run.id} returned an invalid unknown status"
                )
            result = RunResult(
                RunOutcome.FAILED,
                "worker lost the typed run state after restart",
                metadata={"worker_status": "unknown"},
            )
        elif not terminal:
            if raw_result is not None:
                raise LifecycleError(
                    f"Typed run {run.id} returned a non-terminal result"
                )
            return None
        elif raw_result is not None:
            if not isinstance(raw_result, dict):
                raise LifecycleError(f"Typed run {run.id} returned malformed result")
            try:
                result = RunResult.from_dict(raw_result)
            except (KeyError, TypeError, ValueError) as error:
                raise LifecycleError(
                    f"Typed run {run.id} returned malformed terminal result"
                ) from error
        else:
            try:
                if backend is not None and backend.capabilities().remote:
                    result = await backend.collect(run.id)
                else:
                    result = await self._collect_run(run)
            except WorkerTransportError:
                # A terminal status is not proof that the result was
                # collected. Preserve the active run and retry collection
                # after a transient connection failure.
                raise
            except Exception as error:
                result = RunResult(
                    RunOutcome.FAILED,
                    "typed operation collection failed",
                    metadata={"error_type": type(error).__name__},
                )

        collected = replace(run, result=result)
        self._store.save_run(collected, expected=run)
        return await self.complete(run.job_id)

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
        if job.state is JobState.SUSPENDED:
            run = self._require_latest_run(job_id)
            if run.state is not RunState.SUSPENDED or run.result is None:
                raise LifecycleError(
                    f"Job {job_id} has no durable suspended lifecycle marker"
                )
            self._raise_cleanup_errors(self._cleanup_execution_capacity(job_id, run))
            return job
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
            draining_run = replace(run, state=RunState.DRAINING)
            self._store.save_job_and_run(
                draining,
                event,
                draining_run,
                expected_job=job,
                expected_run=run,
            )
            run = draining_run
            job = draining

        if job.state is JobState.DRAINING:
            await self._steer_run(
                run,
                "Stop at the next safe boundary and preserve the supplied "
                "resume state.",
            )
            if not driver.capabilities().native_pause:
                # Turn-boundary adapters (including Codex) deliver steering
                # during collection. Let that bounded turn finish before
                # claiming a durable safe checkpoint; interrupting here would
                # discard the queued instruction.
                result = run.result or await self._collect_run(run)
                result = self._result_with_workspace_commit(run, result)
                collected_run = replace(run, result=result)
                self._store.save_run(collected_run, expected=run)
                run = collected_run
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
            checkpointed_run = replace(run, state=RunState.CHECKPOINTED)
            self._store.save_job_and_run(
                checkpointed,
                event,
                checkpointed_run,
                expected_job=job,
                expected_run=run,
            )
            run = checkpointed_run
            job = checkpointed

        result = run.result
        if result is None:
            await self._interrupt_run(run)
            result = await self._collect_run(run)
        result = self._result_with_workspace_commit(run, result)
        suspended, event = transition_job(
            job,
            JobState.SUSPENDED,
            "checkpoint durable; execution capacity cleanup pending",
        )
        final_run = replace(
            run,
            state=RunState.SUSPENDED,
            ended_at=utc_now(),
            result=result,
        )
        self._store.save_job_and_run(
            suspended,
            event,
            final_run,
            expected_job=job,
            expected_run=run,
        )
        self._raise_cleanup_errors(self._cleanup_execution_capacity(job_id, final_run))
        return suspended

    async def resume(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state is not JobState.SUSPENDED:
            raise LifecycleError(f"Job {job_id} is not suspended")
        maximum = job.quota_budget.maximum
        if maximum is not None and self._job_consumed(job.id) >= maximum - 1e-9:
            raise LifecycleError(f"Job {job_id} exhausted its cumulative quota maximum")
        return self._transition(
            job,
            JobState.READY,
            "resume requested; queued for a new run attempt",
        )

    async def request_review(self, job_id: str) -> Job:
        job = self._store.get_job(job_id)
        if job.state is JobState.REVIEW:
            run = self._require_latest_run(job_id)
            if run.state is not RunState.SUSPENDED or run.result is None:
                raise LifecycleError(
                    f"Job {job_id} has no durable review lifecycle marker"
                )
            self._raise_cleanup_errors(self._cleanup_execution_capacity(job_id, run))
            return job
        if job.state is not JobState.RUNNING:
            raise LifecycleError(f"Job {job_id} is not running")
        run = self._require_active_run(job_id)
        await self._interrupt_run(run)
        result = run.result or await self._collect_run(run)
        result = self._review_handoff_result(result)
        result = self._result_with_workspace_commit(run, result)
        review, event = transition_job(
            job,
            JobState.REVIEW,
            f"run {run.id} handed off for review",
        )
        final_run = replace(
            run,
            state=RunState.SUSPENDED,
            ended_at=utc_now(),
            result=result,
        )
        self._store.save_job_and_run(
            review,
            event,
            final_run,
            expected_job=job,
            expected_run=run,
        )
        self._raise_cleanup_errors(self._cleanup_execution_capacity(job_id, final_run))
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
        self._store.save_job_and_run(
            review,
            event,
            final_run,
            expected_job=job,
            expected_run=run,
        )
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
                        cancelled=job.state is JobState.CANCELLED,
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
            result = await self._collect_run(run)
            result = self._result_with_workspace_commit(run, result)
            collected_run = replace(run, result=result)
            # Collect is an external effect. Persist it while the run remains
            # retryable, before attempting the atomic terminal transition.
            self._store.save_run(collected_run, expected=run)
            run = collected_run
        else:
            # A worker-reported Git commit is not an attestation. Re-read the
            # controller-owned worktree even when an earlier review/checkpoint
            # already persisted the result.
            result = self._result_with_workspace_commit(run, result)

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
        artifacts = self._completion_artifacts(completed, final_run)
        if artifacts:
            self._store.save_job_and_run_with_artifacts(
                completed,
                event,
                final_run,
                artifacts,
                expected_job=job,
                expected_run=run,
            )
        else:
            self._store.save_job_and_run(
                completed,
                event,
                final_run,
                expected_job=job,
                expected_run=run,
            )
        self._raise_cleanup_errors(
            self._cleanup_job_resources(
                job_id,
                final_run,
                completed=target is JobState.COMPLETED,
                cancelled=target is JobState.CANCELLED,
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
                self._cleanup_job_resources(
                    job_id,
                    run,
                    completed=False,
                    cancelled=job.state is JobState.CANCELLED,
                )
            )
            return job

        collected: RunResult | None = run.result if run is not None else None
        if run is not None and run.state in {
            RunState.STARTING,
            RunState.RUNNING,
            RunState.DRAINING,
            RunState.CHECKPOINTED,
        }:
            try:
                await self._cancel_run(run)
                collected = await self._collect_run(run)
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
        cancelled, event = transition_job(job, JobState.CANCELLED, "cancel requested")
        if run is not None:
            final_run = replace(
                run,
                state=RunState.CANCELLED,
                ended_at=utc_now(),
                result=result,
            )
            self._store.save_job_and_run(
                cancelled,
                event,
                final_run,
                expected_job=job,
                expected_run=run,
            )
        else:
            final_run = None
            self._store.save_job(cancelled, event, expected=job)
        cleanup_errors = self._cleanup_job_resources(
            job_id,
            final_run,
            completed=False,
            cancelled=True,
        )
        self._raise_cleanup_errors(cleanup_errors)
        return cancelled

    def execution_contract(self, run_id: str) -> ExecutionContract:
        return self._store.get_run(run_id).contract

    def is_managed_run(self, run_id: str) -> bool:
        run = self._store.get_run(run_id)
        return isinstance(self._drivers.get(run.driver), ManagedHarnessDriver)

    def _transition(self, job: Job, state: JobState, reason: str) -> Job:
        updated, event = transition_job(job, state, reason)
        self._store.save_job(updated, event, expected=job)
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

    def _backend_for_run(self, run: RunRecord) -> WorkerBackend | None:
        if run.backend == "direct":
            return None
        if self._backends is None:
            raise LifecycleError(
                f"Run {run.id} references unavailable backend {run.backend!r}"
            )
        try:
            return self._backends.get(run.backend)
        except LookupError as error:
            raise LifecycleError(
                f"Run {run.id} references unavailable backend {run.backend!r}"
            ) from error

    def _is_remote_run(self, run: RunRecord) -> bool:
        backend = self._backend_for_run(run)
        return backend is not None and backend.capabilities().remote

    async def _observe_run(self, run: RunRecord) -> RunObservation | None:
        backend = self._backend_for_run(run)
        if backend is not None and backend.capabilities().remote:
            return await backend.observe(run.id)
        driver = self._drivers.get(run.driver)
        if not isinstance(driver, ManagedHarnessDriver):
            return None
        return driver.observe(run.id)

    async def _steer_run(self, run: RunRecord, instruction: str) -> None:
        backend = self._backend_for_run(run)
        if backend is not None and backend.capabilities().remote:
            await backend.steer(run.id, instruction)
            return
        await self._drivers.get(run.driver).steer(run.handle, instruction)

    async def _interrupt_run(self, run: RunRecord) -> None:
        backend = self._backend_for_run(run)
        if backend is not None and backend.capabilities().remote:
            await backend.interrupt(run.id)
            return
        await self._drivers.get(run.driver).interrupt(run.handle)

    async def _cancel_run(self, run: RunRecord) -> None:
        backend = self._backend_for_run(run)
        if backend is not None and backend.capabilities().remote:
            await backend.cancel(run.id)
            return
        await self._drivers.get(run.driver).cancel(run.handle)

    async def _collect_run(self, run: RunRecord) -> RunResult:
        backend = self._backend_for_run(run)
        if backend is not None and backend.capabilities().remote:
            return await backend.collect(run.id)
        return await self._drivers.get(run.driver).collect(run.handle)

    def _create_remote_driver_session(
        self,
        run: RunRecord,
        handle: RunHandle,
    ) -> None:
        self._store.save_driver_session(
            DriverSession(
                run_id=run.id,
                driver=run.driver,
                external_id=handle.external_id or handle.id,
                metadata={"backend": run.backend, "remote": True},
            ),
            expected=None,
        )

    def _record_remote_observation(
        self,
        run: RunRecord,
        observation: RunObservation,
    ) -> None:
        """Mirror worker telemetry into the controller's durable quota ledger."""

        if observation.run_id != run.id:
            raise LifecycleError("Remote observation belongs to another run")
        session = self._store.get_driver_session(run.id)
        if session.observation_cursor == observation.cursor:
            return
        if observation.telemetry_valid:
            cumulative = observation.normalized_cumulative_quota
            if cumulative is not None and cumulative > 0:
                prior = self._store.list_usage_samples(run.id)
                sequence = max((sample.sequence for sample in prior), default=-1) + 1
                self._store.apply_usage_sample(
                    observation.to_usage_sample(sequence),
                    maximum=self._store.get_job(run.job_id).quota_budget.maximum,
                )
        self._store.update_observation_cursor(
            run.id,
            session.observation_cursor,
            observation.cursor,
            observation,
        )

    async def _deliver_remote_commands(
        self,
        run: RunRecord,
        observation: RunObservation,
    ) -> None:
        for command in self._store.list_pending_run_commands(run.id):
            if command.action == "repair":
                continue
            if command.action in {"steer", "checkpoint", "suspend"}:
                supplied = command.payload.get("instruction")
                if isinstance(supplied, str) and supplied.strip():
                    instruction = supplied.strip()
                elif command.action == "checkpoint":
                    instruction = (
                        "Stop at the next safe boundary and return a durable "
                        "checkpoint with completed work, current state, next "
                        "steps, known failures, and decisions."
                    )
                elif command.action == "suspend":
                    instruction = (
                        "Stop at the next safe boundary and return the durable "
                        "state needed to resume later."
                    )
                else:
                    raise LifecycleError(
                        f"Remote steer command {command.id} has no instruction"
                    )
                await self._steer_run(
                    run,
                    f"{instruction}\n\nControl-plane command id: {command.id}",
                )
            elif command.action == "interrupt":
                await self._interrupt_run(run)
            elif command.action == "cancel":
                await self._cancel_run(run)
            else:
                raise LifecycleError(
                    f"Unsupported remote run command {command.action!r}"
                )
            self._store.acknowledge_run_command(
                RunCommandAck(
                    command_id=command.id,
                    run_id=run.id,
                    observation_cursor=observation.cursor,
                    metadata={"backend": run.backend, "remote": True},
                )
            )

    def _build_contract(
        self,
        job: Job,
        workspace: WorkspaceLease,
        *,
        artifact_inputs: tuple[ArtifactRef, ...] | None = None,
        operation: BuildImageOperation | DeployImageOperation | None = None,
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
        resolved_inputs = (
            artifact_inputs
            if artifact_inputs is not None
            else self._resolve_artifacts(job)[0]
        )
        resolved_operation = (
            operation if operation is not None else self._resolve_artifacts(job)[1]
        )
        typed_operation = self._is_operation_workspace(workspace)
        return ExecutionContract(
            job_id=job.id,
            objective=job.objective,
            scope=f"Project {job.project} in repository {job.repository}",
            acceptance_criteria=job.acceptance_criteria,
            dependency_results=dependency_results,
            role="implementation worker",
            allowed_filesystem_scope=(
                () if typed_operation else (workspace.working_directory,)
            ),
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
            artifact_inputs=resolved_inputs,
            artifact_outputs=job.artifact_outputs,
            operation=resolved_operation,
        )

    def _resolve_artifacts(
        self,
        job: Job,
    ) -> tuple[
        tuple[ArtifactRef, ...],
        BuildImageOperation | DeployImageOperation | None,
    ]:
        """Bind every selector/ref to verified append-only ledger provenance."""

        ledger = tuple(self._store.list_artifacts())

        def resolve(value: ArtifactInput) -> ArtifactRef:
            if isinstance(value, ArtifactRef):
                matches = [
                    item for item in ledger if item.ref == value and item.verified
                ]
                if not matches:
                    raise _ArtifactUnavailable(
                        f"Job {job.id} is waiting for a verified immutable artifact"
                    )
                # Several producer jobs may legitimately converge on the same
                # content-addressed Git/OCI value. Direct refs resolve to the
                # value, not to one arbitrary provenance row.
                return value
            else:
                matches = [
                    item
                    for item in ledger
                    if item.producer_job_id == value.producer_job_id
                    and item.spec_name == value.spec_name
                    and item.ref.kind is value.kind
                    and item.verified
                ]
            if len(matches) != 1:
                raise _ArtifactUnavailable(
                    f"Job {job.id} is waiting for one verified immutable artifact"
                )
            return matches[0].ref

        resolved_inputs = tuple(resolve(value) for value in job.artifact_inputs)
        operation = job.operation
        if isinstance(operation, BuildImageOperation):
            operation = replace(operation, source_input=resolve(operation.source_input))
        elif isinstance(operation, DeployImageOperation):
            operation = replace(operation, image_input=resolve(operation.image_input))
            # Configuration revisions are direct immutable inputs too, even
            # though the operation keeps the field separate for type clarity.
            resolve(operation.config_revision)
        return resolved_inputs, operation

    @staticmethod
    def _operation_workspace(
        job: Job,
        placement: Placement,
        backend: WorkerBackend | None,
    ) -> WorkspaceLease:
        backend_name = backend.capabilities().name if backend is not None else "direct"
        return WorkspaceLease(
            id=f"operation-workspace:{new_id()}",
            job_id=job.id,
            repository=job.repository,
            branch=f"agentd/remote/{job.id}",
            working_directory=f"worker://{placement.node_id}/{job.id}",
            base_ref=job.base_ref,
            environment={},
            runtime_namespace=f"operation:{backend_name}",
        )

    @staticmethod
    def _is_operation_workspace(workspace: WorkspaceLease) -> bool:
        return bool(
            workspace.runtime_namespace
            and workspace.runtime_namespace.startswith("operation:")
        )

    def _release_operation_workspace(
        self,
        workspace: WorkspaceLease,
    ) -> WorkspaceLease:
        if workspace.state is WorkspaceState.RELEASED:
            return workspace
        released = replace(
            workspace,
            state=WorkspaceState.RELEASED,
            released_at=utc_now(),
        )
        self._store.save_workspace(released, expected=workspace)
        return released

    async def _prepare_recovery_workspace(
        self,
        job: Job,
        run: RunRecord,
    ) -> WorkspaceLease:
        """Re-establish trusted workspace ownership before opening transport."""

        workspace = self._store.get_workspace(run.workspace_id)
        registered = self._store.find_workspace(job.id)
        if (
            workspace.job_id != job.id
            or workspace.repository != job.repository
            or workspace.state is not WorkspaceState.LEASED
            or registered is None
            or registered.id != workspace.id
        ):
            raise LifecycleError(
                f"Run {run.id} does not own the registered leased workspace"
            )
        if (
            run.contract.job_id != job.id
            or run.contract.working_directory != workspace.working_directory
            or run.contract.allowed_filesystem_scope != (workspace.working_directory,)
            or run.contract.environment != workspace.environment
        ):
            raise LifecycleError(
                f"Run {run.id} execution contract no longer matches its workspace"
            )
        if not self._workspaces.is_available(workspace):
            raise LifecycleError(
                f"Run {run.id} workspace ownership or branch is unavailable"
            )
        if self._provisioner is not None:
            await self._provisioner.prepare(workspace)
        return workspace

    def _base_ref(self, job: Job) -> str:
        latest = self._store.latest_checkpoint(job.id)
        if latest is not None and latest.capsule.commit:
            return latest.capsule.commit
        for dependency_id in reversed(job.dependencies):
            runs = self._store.list_runs(dependency_id)
            if runs and runs[-1].result and runs[-1].result.commit:
                return runs[-1].result.commit
        return job.base_ref

    def _job_consumed(self, job_id: str) -> float:
        return sum(
            reservation.consumed
            for reservation in self._store.list_reservations(job_id)
        )

    def _job_for_next_reservation(self, job: Job) -> Job:
        """Cap this attempt without changing the job's cumulative policy."""

        maximum = job.quota_budget.maximum
        if maximum is None:
            return job
        remaining = maximum - self._job_consumed(job.id)
        if remaining <= 1e-9:
            raise QuotaAdmissionError(
                f"Job {job.id} exhausted its cumulative quota maximum"
            )
        amount = min(job.quota_budget.expected_path, remaining)
        attempt_budget = replace(
            job.quota_budget,
            implementation=amount,
            review=0,
            repair=0,
            validation=0,
        )
        return replace(job, quota_budget=attempt_budget)

    def _capsule_with_workspace_commit(
        self,
        run: RunRecord,
        capsule: ResumeCapsule,
    ) -> ResumeCapsule:
        if capsule.commit is not None or run.contract.operation is not None:
            return capsule
        workspace = self._store.get_workspace(run.workspace_id)
        return replace(capsule, commit=self._workspaces.current_commit(workspace))

    def _result_with_workspace_commit(
        self,
        run: RunRecord,
        result: RunResult,
    ) -> RunResult:
        if run.contract.operation is not None:
            return result
        return self._trusted_workspace_result(run, result)

    def _completion_artifacts(
        self,
        job: Job,
        run: RunRecord,
    ) -> tuple[ArtifactRecord, ...]:
        """Validate declared outputs and construct one atomic ledger batch."""

        if run.result is None:
            raise LifecycleError(f"Run {run.id} has no result to publish")
        produced = run.result.produced_artifacts
        if job.state is not JobState.COMPLETED:
            return ()
        expected = {spec.name: spec for spec in job.artifact_outputs}
        actual = {item.spec_name: item for item in produced}
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            unexpected = sorted(set(actual) - set(expected))
            raise LifecycleError(
                "Completed run outputs do not match the declared artifact slots "
                f"(missing={missing}, unexpected={unexpected})"
            )
        for name, item in actual.items():
            if item.ref.kind is not expected[name].kind:
                raise LifecycleError(
                    f"Artifact output {name!r} has kind {item.ref.kind.value!r}; "
                    f"expected {expected[name].kind.value!r}"
                )
            if item.ref.kind is ArtifactKind.GIT_COMMIT:
                if run.result.commit is None:
                    raise LifecycleError(
                        f"Artifact output {name!r} has no trusted workspace commit"
                    )
                trusted_ref = ArtifactRef(
                    ArtifactKind.GIT_COMMIT,
                    run.result.commit.lower(),
                )
                if item.ref != trusted_ref:
                    raise LifecycleError(
                        f"Artifact output {name!r} does not match the trusted "
                        "workspace commit"
                    )

        image_outputs = [
            item for item in actual.values() if item.ref.kind is ArtifactKind.OCI_IMAGE
        ]
        if image_outputs:
            operation = run.contract.operation
            if not isinstance(operation, BuildImageOperation):
                raise LifecycleError(
                    "OCI artifact outputs require a typed Build operation"
                )
            backend = self._backend_for_run(run)
            features = (
                backend.capabilities().features
                if backend is not None and backend.capabilities().remote
                else self._drivers.get(run.driver).capabilities().features
            )
            if ARTIFACT_VERIFICATION_FEATURE not in features:
                raise LifecycleError(
                    "Build output cannot be published without trusted artifact "
                    "verification"
                )
            for item in image_outputs:
                repository, _, _digest = item.ref.value.partition("@")
                if repository != operation.registry_repository:
                    raise LifecycleError(
                        f"Artifact output {item.spec_name!r} targets repository "
                        f"{repository!r}; expected {operation.registry_repository!r}"
                    )
        published_at = run.ended_at or utc_now()
        return tuple(
            ArtifactRecord(
                id=f"artifact:{run.id}:{name}",
                ref=actual[name].ref,
                producer_job_id=job.id,
                producer_run_id=run.id,
                spec_name=name,
                verified=True,
                verified_at=published_at,
                created_at=published_at,
                metadata=actual[name].metadata,
            )
            for name in sorted(expected)
        )

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
                if self._is_operation_workspace(workspace):
                    self._release_operation_workspace(workspace)
                else:
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

    def _cleanup_execution_capacity(
        self,
        job_id: str,
        run: RunRecord,
        *,
        cancelled: bool = False,
    ) -> list[BaseException]:
        """Release retry-safe allocation/quota effects while retaining workspace."""

        errors: list[BaseException] = []
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
                        run.result.consumed_quota if run.result is not None else 0
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
        placements: list[tuple[Placement, WorkerBackend | None]] = []
        for placement in compatible_placements(
            job,
            self._store.list_nodes(),
            capabilities,
        ):
            if placement.harness != run.driver or not self._provider_allows_placement(
                job, placement
            ):
                continue
            backend = self._select_backend(placement.node_id, job)
            if self._backends is not None and backend is None:
                continue
            placements.append((placement, backend))
        if not placements:
            raise QuotaAdmissionError(
                "No policy-compliant capacity is available for repair"
            )
        placement, _backend = placements[0]

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
        published_run: RunRecord | None = None
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
            self._store.save_job_and_run(
                running,
                event,
                starting,
                expected_job=job,
                expected_run=run,
            )
            published_run = starting
            handle = await driver.continue_turn(run.id, instruction.strip())
            active = replace(starting, handle=handle, state=RunState.RUNNING)
            self._store.save_run(active, expected=starting)
            published_run = active
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
                if running is not None and published_run is not None:
                    review, event = transition_job(
                        running,
                        JobState.REVIEW,
                        "repair start failed; reviewed handoff restored",
                    )
                    self._store.save_job_and_run(
                        review,
                        event,
                        run,
                        expected_job=running,
                        expected_run=published_run,
                    )
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
        observation: RunObservation | None = None,
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
        if maximum_command is not None:
            maximum_command = replace(
                maximum_command,
                id=f"{maximum_command.id}:{run.id}",
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
            stop_command = provider_stop_command(
                snapshot,
                run_id=run.id,
                at=at,
                policy=self._provider_stop_policy,
                account_policy=self._account_policy,
            )
            if stop_command is not None and not self._job_has_command(
                job.id,
                stop_command.id,
            ):
                self._store.enqueue_run_command(stop_command)

        tail = tail_governor_command(
            job,
            run,
            at=at,
            observation=observation,
        )
        if tail is not None:
            _decision, tail_command = tail
            if tail_command is not None and not self._job_has_command(
                job.id,
                tail_command.id,
            ):
                self._store.enqueue_run_command(tail_command)

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
                interrupt = replace(interrupt, id=f"{interrupt.id}:{run.id}")
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
        if telemetry_valid and result.outcome is RunOutcome.COMPLETED:
            result = self._trusted_workspace_commit_result(run, result)

        commands = self._store.list_run_commands(run.id)
        job_consumed = self._job_consumed(job.id)
        suspension_requested = any(
            command.action in {"checkpoint", "suspend"} for command in commands
        ) or should_checkpoint_for_maximum(
            job_consumed,
            job.quota_budget.maximum,
            self._usage_policy,
        )
        if not telemetry_valid:
            target = JobState.METERING_PENDING
        elif suspension_requested:
            target = JobState.SUSPENDED
        elif result.outcome is RunOutcome.COMPLETED:
            target = JobState.REVIEW
        elif result.outcome is RunOutcome.CANCELLED:
            target = JobState.CANCELLED
        else:
            target = JobState.FAILED
        result = replace(
            result,
            metadata={
                **result.metadata,
                _MANAGED_TELEMETRY_VALID: telemetry_valid,
                _MANAGED_FINALIZATION_TARGET: target.value,
                _MANAGED_FINALIZATION_TURN: observation.turn_id,
            },
        )

        pending_run = replace(
            run,
            state=RunState.SUSPENDED,
            ended_at=utc_now(),
            result=result,
        )
        reason = (
            "Codex turn quiesced; terminal usage reconciliation pending"
            if telemetry_valid
            else (
                "Codex turn ended without valid terminal token telemetry; "
                "acceptance blocked pending reconciliation"
            )
        )
        self._quota.begin_metering(
            job.id,
            reason=reason,
            run=pending_run,
            expected_run=run,
        )
        job = self._store.get_job(job.id)
        run = pending_run
        return self._resume_managed_finalization(job, run)

    def _resume_managed_finalization(self, job: Job, run: RunRecord) -> Job:
        """Complete or safely hold one durable managed metering marker."""

        if job.state is not JobState.METERING_PENDING:
            return job
        if run.state is not RunState.SUSPENDED or run.result is None:
            raise LifecycleError(
                f"Job {job.id} has a malformed managed metering marker"
            )
        telemetry_valid = run.result.metadata.get(_MANAGED_TELEMETRY_VALID)
        target_value = run.result.metadata.get(_MANAGED_FINALIZATION_TARGET)
        turn_id = run.result.metadata.get(_MANAGED_FINALIZATION_TURN)
        if not isinstance(telemetry_valid, bool) or not isinstance(target_value, str):
            raise LifecycleError(
                f"Job {job.id} has an incomplete managed metering marker"
            )

        allocation = self._store.find_active_allocation(job.id)
        if allocation is not None:
            self._resources.release(allocation.id)
        if not telemetry_valid:
            if target_value != JobState.METERING_PENDING.value:
                raise LifecycleError(
                    f"Job {job.id} has an inconsistent invalid-telemetry marker"
                )
            reservation = self._store.get_reservation(run.reservation_id)
            if reservation.state is not ReservationState.METERING_PENDING:
                raise LifecycleError(
                    f"Job {job.id} invalid telemetry no longer holds quota"
                )
            return job

        try:
            target = JobState(target_value)
        except ValueError as error:
            raise LifecycleError(
                f"Job {job.id} has an unknown managed finalization target"
            ) from error
        if target not in {
            JobState.REVIEW,
            JobState.SUSPENDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            raise LifecycleError(
                f"Job {job.id} has an invalid managed finalization target {target}"
            )

        reservation = self._store.get_reservation(run.reservation_id)
        if reservation.state is ReservationState.METERING_PENDING:
            reservation = self._quota.settle(
                reservation.id,
                cancelled=target is JobState.CANCELLED,
            )
        expected_reservation_state = (
            ReservationState.CANCELLED
            if target is JobState.CANCELLED
            else ReservationState.RELEASED
        )
        if reservation.state is not expected_reservation_state:
            raise LifecycleError(
                f"Job {job.id} quota settlement does not match its finalization target"
            )

        if target is JobState.SUSPENDED:
            return self._finalize_managed_suspension(job, run, run.result)

        reason_turn = turn_id if isinstance(turn_id, str) and turn_id else "unknown"
        reason = (
            f"Codex turn {reason_turn} is ready for explicit review"
            if target is JobState.REVIEW
            else f"Codex turn {reason_turn} reported {run.result.outcome.value}"
        )
        final, event = transition_job(job, target, reason)
        final_run = replace(
            run,
            state=(
                RunState.SUSPENDED
                if target is JobState.REVIEW
                else (
                    RunState.CANCELLED
                    if target is JobState.CANCELLED
                    else RunState.FAILED
                )
            ),
            ended_at=run.ended_at or utc_now(),
        )
        self._store.save_job_and_run(
            final,
            event,
            final_run,
            expected_job=job,
            expected_run=run,
        )
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
        self._store.save_job_and_run(
            suspended,
            event,
            final_run,
            expected_job=job,
            expected_run=run,
        )
        return suspended

    def _trusted_workspace_result(
        self,
        run: RunRecord,
        result: RunResult,
    ) -> RunResult:
        workspace = self._store.get_workspace(run.workspace_id)
        current = ArtifactRef(
            ArtifactKind.GIT_COMMIT,
            self._workspaces.current_commit(workspace).lower(),
        ).value
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
        commit = ArtifactRef(
            ArtifactKind.GIT_COMMIT,
            self._workspaces.commit_changes(workspace).lower(),
        ).value
        if workspace.commit != commit:
            self._store.save_workspace(
                replace(workspace, commit=commit),
                expected=workspace,
            )
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
        target = JobState.READY if job.state is JobState.ADMITTED else JobState.FAILED
        updated, event = transition_job(
            job,
            target,
            "startup reconciliation found no durable managed-driver session",
        )
        self._store.save_job_and_run(
            updated,
            event,
            cancelled_run,
            expected_job=job,
            expected_run=run,
        )
        self._raise_cleanup_errors(
            self._cleanup_job_resources(job.id, cancelled_run, completed=False)
        )

    def _job_has_command(self, job_id: str, command_id: str) -> bool:
        return any(
            command.id == command_id
            for run in self._store.list_runs(job_id)
            for command in self._store.list_run_commands(run.id)
        )

    def _select_backend(self, node_id: str, job: Job) -> WorkerBackend | None:
        if self._backends is None:
            return None
        compatible = self._backends.compatible(self._store.get_node(node_id))
        for backend in compatible:
            capabilities = backend.capabilities()
            if capabilities.remote and job.operation is None:
                # A remote code harness also needs an authenticated remote
                # source/workspace lifecycle.  This MVP's distributed path is
                # deliberately artifact-centric; never hand a controller-local
                # worktree path to a remote process.
                continue
            if (
                capabilities.remote
                and isinstance(job.operation, BuildImageOperation)
                and (
                    "build-image" not in capabilities.features
                    or ARTIFACT_VERIFICATION_FEATURE not in capabilities.features
                )
            ):
                continue
            if (
                capabilities.remote
                and isinstance(job.operation, DeployImageOperation)
                and "deploy-image" not in capabilities.features
            ):
                continue
            return backend
        return None

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
            self._store.save_workspace(retained, expected=workspace)
            return retained
        self._store.save_workspace(released, expected=workspace)
        return released
