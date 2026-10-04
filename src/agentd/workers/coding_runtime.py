"""Compose the existing SDK/supervisor behind the deployed sandbox preflight.

This is a trusted worker bootstrap API, not a protocol operation. Passing a
boolean never grants containment features: the deployed runtime proof must exit
successfully under the same environment used to start the SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from agentd.coding.models import CodingWorkOrder
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome, RunState
from agentd.domain.models import (
    CodingOperation,
    EffortEstimate,
    ExecutionContract,
    HarnessCapabilities,
    Job,
    QuotaBudget,
    QuotaPool,
    QuotaReservation,
    ResourceAllocation,
    ResourceVector,
    RunHandle,
    RunObservation,
    RunRecord,
    RunResult,
    StateTransition,
    TokenUsage,
    WorkerNode,
    WorkspaceLease,
)
from agentd.harness.app_server import DEFAULT_CODEX_MODEL, OpenAICodexClient
from agentd.harness.codex_sdk import CodexSdkDriver
from agentd.harness.native_record import read_native_terminal_usage
from agentd.harness.supervisor import RunSupervisor
from agentd.lifecycle import ControllerLock
from agentd.state.base import EntityNotFoundError
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.dependency_prep import (
    DependencyPreparationError,
    DependencyRuntimePreparation,
    PreparationMount,
)
from agentd.workers.operations import OperationError, SubprocessCommandRunner
from agentd.workers.remote_protocol import canonical_json, payload_hash

_RUNTIME_PYTHON = "/opt/agentd/venv/bin/python"
_RUNTIME_PROBE = "/opt/agentd/security/runtime_sandbox_probe.py"


class CodingPreparationError(OperationError):
    """The local execution envelope failed before any provider call."""


class _ContainedCodexDriver(CodexSdkDriver):
    """SDK with a worker-local execution-envelope ledger, never a quota oracle."""

    def __init__(
        self,
        supervisor: RunSupervisor,
        store: SQLiteStateStore,
        *,
        model: str,
        dependency_venv: Path | None = None,
        dependency_profile_id: str | None = None,
        dependency_python_root: Path | None = None,
        worker_owner: ControllerLock | None = None,
        native_codex_home: Path | None = None,
    ) -> None:
        super().__init__(supervisor, model=model)
        self._worker_store = store
        self._dependency_venv = dependency_venv
        self._dependency_profile_id = dependency_profile_id
        self._dependency_python_root = dependency_python_root
        self._worker_owner = worker_owner
        self._native_codex_home = native_codex_home

    def _require_worker_owner(self) -> None:
        expected_owner = ControllerLock(
            Path(self._worker_store.path).resolve().parent / "worker.sqlite"
        )
        if (
            self._worker_owner is None
            or not self._worker_owner.owned
            or self._worker_owner.path != expected_owner.path
        ):
            raise OperationError("recovery requires exclusive worker ownership")

    async def recover_terminal_readonly(
        self, run_id: str, order: CodingWorkOrder, lease: WorkspaceLease
    ) -> RunObservation | None:
        """Recover saved final usage after the old worker owner has exited."""
        self._require_worker_owner()
        run = self._worker_store.get_run(run_id)
        if (
            run.job_id != f"worker-envelope:{run_id}"
            or not isinstance(run.contract.operation, CodingOperation)
            or run.contract.operation.work_order != order
            or run.contract.working_directory != lease.working_directory
        ):
            raise OperationError("terminal recovery envelope identity mismatch")
        session = self._worker_store.get_driver_session(run_id)
        runtime_version = session.metadata.get("runtime_version")
        reader = None
        if self._native_codex_home is not None and isinstance(runtime_version, str):
            reader = partial(
                read_native_terminal_usage,
                codex_home=self._native_codex_home,
                runtime_version=runtime_version,
            )
        return await self._supervisor.recover_terminal_readonly(
            run_id, terminal_usage_reader=reader
        )

    def prove_physical_quarantine(
        self,
        run_id: str,
        order: CodingWorkOrder | None,
        lease: WorkspaceLease | None,
        *,
        actor: str,
        event_id: str,
        stop_proof: dict,
        start_hash: str,
    ) -> dict:
        """Verify a protected host attestation after the old container stopped.

        Kernel ownership alone cannot prove orphaned descendants stopped. The
        trusted host must first inspect the exact stopped Docker container and
        install this attestation before the replacement worker acquires its
        startup fence. This method never invokes the SDK or provider.
        """
        self._require_worker_owner()
        acquired = self._worker_owner.acquired_at
        if acquired is None or run_id in self._supervisor._live:
            raise OperationError("quarantine cannot retire a live SDK owner")
        keys = {
            "proof_version",
            "node_id",
            "session_epoch",
            "run_id",
            "job_id",
            "start_hash",
            "actor",
            "event_id",
            "container_id",
            "stopped_at",
            "running",
            "pid",
        }
        if (
            set(stop_proof) != keys
            or stop_proof.get("proof_version") != 1
            or isinstance(stop_proof.get("proof_version"), bool)
            or stop_proof.get("running") is not False
            or stop_proof.get("pid") != 0
            or isinstance(stop_proof.get("pid"), bool)
            or any(
                not isinstance(stop_proof.get(key), str)
                or not stop_proof[key]
                or len(stop_proof[key]) > 256
                or "\0" in stop_proof[key]
                for key in keys - {"proof_version", "running", "pid"}
            )
            or stop_proof["run_id"] != run_id
            or stop_proof["actor"] != actor
            or stop_proof["event_id"] != event_id
            or stop_proof["start_hash"] != start_hash
            or (order is not None and stop_proof["job_id"] != order.job_id)
            or len(stop_proof["container_id"]) != 64
            or any(c not in "0123456789abcdef" for c in stop_proof["container_id"])
        ):
            raise OperationError("quarantine stop attestation identity mismatch")
        try:
            stopped = datetime.fromisoformat(stop_proof["stopped_at"])
        except ValueError as error:
            raise OperationError("quarantine stop time is malformed") from error
        if stopped.utcoffset() != timedelta(0) or stopped > acquired:
            raise OperationError(
                "host stop must precede this exact worker startup fence"
            )
        try:
            run = self._worker_store.get_run(run_id)
        except EntityNotFoundError:
            run = None
        if run is not None and (
            order is None
            or lease is None
            or run.job_id != f"worker-envelope:{run_id}"
            or not isinstance(run.contract.operation, CodingOperation)
            or run.contract.operation.work_order != order
            or run.contract.working_directory != lease.working_directory
            or run.started_at > stopped
        ):
            raise OperationError("quarantine execution envelope identity mismatch")
        if any(
            sample.final for sample in self._worker_store.list_usage_samples(run_id)
        ):
            raise OperationError("final SDK usage exists; use terminal recovery")
        directory = Path(self._worker_store.path).resolve().parent / "quarantine-stops"
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OperationError("quarantine proof directory is not protected")
        name = hashlib.sha256(
            canonical_json({"run_id": run_id, "event_id": event_id})
        ).hexdigest()
        fd = os.open(directory / f"{name}.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 16_384
                or metadata.st_mtime > acquired.timestamp()
            ):
                raise OperationError(
                    "quarantine stop proof is not a protected regular file"
                )
            with os.fdopen(fd, "rb", closefd=False) as stream:
                protected = json.loads(stream.read(16_385))
        finally:
            os.close(fd)
        if canonical_json(protected) != canonical_json(stop_proof):
            raise OperationError("quarantine payload lacks the actual host stop proof")
        return {
            "proof_version": 1,
            "run_id": run_id,
            "job_id": stop_proof["job_id"],
            "node_id": stop_proof["node_id"],
            "session_epoch": stop_proof["session_epoch"],
            "start_hash": start_hash,
            "actor": actor,
            "event_id": event_id,
            "physical_retired": True,
            "metering_unknown": True,
            "stop_proof_sha256": payload_hash(stop_proof),
            "container_id": stop_proof["container_id"],
            "stopped_at": stop_proof["stopped_at"],
            "fenced_at": acquired.isoformat(),
        }

    def capabilities(self) -> HarnessCapabilities:
        capabilities = super().capabilities()
        return replace(
            capabilities,
            features=capabilities.features
            | {
                "credential-isolated",
                "network-disabled",
            },
        )

    async def start_managed(
        self, run_id: str, execution: ExecutionContract
    ) -> RunHandle:
        if not isinstance(execution.operation, CodingOperation):
            raise OperationError("contained SDK requires a typed coding order")
        order = execution.operation.work_order
        if (
            self._dependency_venv is not None
            and order.profile_id == self._dependency_profile_id
        ):
            preparation = DependencyRuntimePreparation(
                self._dependency_venv.parent,
                (PreparationMount(self._dependency_venv, ".venv"),),
                (self._dependency_python_root,) if self._dependency_python_root else (),
            )
            try:
                await asyncio.to_thread(
                    preparation.prepare, Path(execution.working_directory)
                )
            except (DependencyPreparationError, OSError) as error:
                raise CodingPreparationError(
                    "worker dependency preparation failed"
                ) from error
        remaining = order.maximum_quota - order.prior_consumed_quota
        # This pool records only the already-admitted execution envelope. It
        # does not represent provider availability or make admission decisions.
        pool_id = f"execution-envelope:{run_id}"
        job = Job(
            # The SDK ledger mirrors an individual admitted envelope. A logical
            # controller job may have many attempts, each with its own active
            # reservation. Keep the logical identity in the immutable contract.
            id=f"worker-envelope:{run_id}",
            project=order.repository,
            repository=execution.working_directory,
            objective=order.objective,
            quota_budget=QuotaBudget(
                min(order.expected_quota, remaining),
                maximum=remaining,
                pool_id=pool_id,
                unit=QuotaUnit.TOKENS,
            ),
            effort=EffortEstimate(1, 1),
            state=JobState.RUNNING,
        )
        reservation = QuotaReservation(
            job.id, pool_id, remaining, unit=QuotaUnit.TOKENS
        )
        workspace = WorkspaceLease(
            id=run_id,
            job_id=job.id,
            repository=execution.working_directory,
            branch="worker-owned",
            working_directory=execution.working_directory,
            base_ref=order.base_commit,
        )
        allocation = ResourceAllocation(
            id=run_id, job_id=job.id, node_id="worker-local", resources=ResourceVector()
        )
        run = RunRecord(
            id=run_id,
            job_id=job.id,
            node_id="worker-local",
            workspace_id=workspace.id,
            reservation_id=reservation.id,
            allocation_id=allocation.id,
            driver="codex",
            backend="local",
            contract=replace(execution, job_id=job.id),
            handle=RunHandle(run_id, "codex"),
        )
        try:
            self._worker_store.prepare_worker_execution(
                job=job,
                transition=StateTransition(
                    job.id,
                    None,
                    JobState.RUNNING,
                    "Worker mirror of controller-admitted execution envelope",
                ),
                pool=QuotaPool(
                    pool_id,
                    "controller-execution-envelope",
                    remaining,
                    reserved=remaining,
                    unit=QuotaUnit.TOKENS,
                ),
                reservation=reservation,
                workspace=workspace,
                node=WorkerNode(
                    "worker-local", {}, ResourceVector(), frozenset({"codex"})
                ),
                allocation=allocation,
                run=run,
            )
        except Exception as error:
            raise CodingPreparationError(
                "worker execution preparation failed"
            ) from error
        # This durable run boundary must precede every SDK/provider entrypoint.
        return await super().start_managed(run_id, run.contract)

    def prove_preparation_failure(
        self, run_id: str, order: CodingWorkOrder, lease: WorkspaceLease
    ) -> RunResult:
        """Administrative proof for legacy partial local setup, never a retry."""
        for lookup in (
            self._worker_store.get_run,
            self._worker_store.get_driver_session,
        ):
            try:
                lookup(run_id)
            except EntityNotFoundError:
                continue
            raise OperationError("provider ownership cannot be ruled out")
        # Legacy setup wrote the worker job/workspace before its run. Require
        # those exact partial identities instead of accepting arbitrary run IDs.
        job = self._worker_store.get_job(order.job_id)
        workspace = self._worker_store.get_workspace(run_id)
        if (
            workspace.job_id != order.job_id
            or job.state is not JobState.RUNNING
            or workspace.working_directory != lease.working_directory
            or workspace.base_ref != order.base_commit
            or job.quota_budget.pool_id != f"execution-envelope:{run_id}"
            or job.quota_budget.maximum != order.maximum_quota
            or job.objective != order.objective
            or job.repository != lease.working_directory
        ):
            raise OperationError("legacy worker preparation identity mismatch")
        return RunResult(
            RunOutcome.FAILED,
            "Worker preparation failed before provider start",
            usage=TokenUsage(),
            metadata={
                "telemetry_valid": True,
                "provider_started": False,
                "preparation_failure": True,
                "preparation_proof": (
                    "SDK run and session absent; partial job/workspace retained"
                ),
            },
        )

    def prove_provider_not_started(
        self,
        run_id: str,
        order: CodingWorkOrder,
        lease: WorkspaceLease,
    ) -> RunResult:
        """Prove a fenced startup stopped before the first provider turn.

        RunSupervisor persists its SDK session before issuing turn/start. A
        session's absence therefore proves zero provider work only while this
        process owns the exact worker journal's exclusive startup fence.
        """
        self._require_worker_owner()
        try:
            self._worker_store.get_driver_session(run_id)
        except EntityNotFoundError:
            pass
        else:
            raise OperationError(
                "provider session exists; ownership and final usage remain unresolved"
            )
        run = self._worker_store.get_run(run_id)
        workspace = self._worker_store.get_workspace(run_id)
        job = self._worker_store.get_job(run.job_id)
        reservation = self._worker_store.get_reservation(run.reservation_id)
        if (
            run.driver != "codex"
            or run.state is not RunState.STARTING
            or run.job_id != f"worker-envelope:{run_id}"
            or not isinstance(run.contract.operation, CodingOperation)
            or run.contract.operation.work_order != order
            or run.contract.working_directory != lease.working_directory
            or workspace.job_id != run.job_id
            or workspace.working_directory != lease.working_directory
            or job.state is not JobState.RUNNING
            or job.objective != order.objective
            or job.repository != lease.working_directory
            or reservation.pool_id != f"execution-envelope:{run_id}"
            or reservation.amount != order.maximum_quota - order.prior_consumed_quota
            or reservation.consumed != 0
            or self._worker_store.list_usage_samples(run_id)
        ):
            raise OperationError("pre-provider recovery envelope identity mismatch")
        return RunResult(
            RunOutcome.FAILED,
            "Worker restarted before the provider turn began",
            usage=TokenUsage(),
            metadata={
                "telemetry_valid": True,
                "provider_started": False,
                "preparation_failure": True,
                "preparation_proof": (
                    "exclusive worker fence; matching atomic envelope; "
                    "SDK session absent"
                ),
            },
        )

    async def close(self) -> None:
        await super().close()
        self._worker_store.close()


async def create_verified_coding_sdk(
    state_path: Path,
    *,
    environment: Mapping[str, str],
    model: str = DEFAULT_CODEX_MODEL,
    dependency_venv: Path | None = None,
    dependency_profile_id: str | None = None,
    dependency_python_root: Path | None = None,
    worker_owner: ControllerLock | None = None,
) -> CodexSdkDriver:
    """Run the image-pinned probe, then grant narrowly proven capabilities.

    Only the existing reviewed Linux container layout is supported. Deployment
    credentials stay in the dedicated auth file whose unreadability the probe
    actually tests. API-key or publication-token environment injection is not
    accepted. State paths must remain beneath the protected deployment state
    root, outside every coding workspace.
    """
    if dependency_venv is not None and not dependency_profile_id:
        raise OperationError(
            "dependency preparation requires an explicit profile identity"
        )
    state_path = state_path.resolve()
    state_root = Path("/home/bened/.local/state/agentd").resolve()
    if not state_path.is_relative_to(state_root):
        raise OperationError("worker SDK state must use the protected deployment root")
    approved = {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR"}
    if set(environment) - approved:
        raise OperationError("worker SDK environment contains unapproved variables")
    runtime_environment = dict(environment)
    if (
        runtime_environment.get("CODEX_HOME")
        != "/home/bened/.local/share/agentd/codex-home"
    ):
        raise OperationError("worker SDK must use the dedicated protected Codex home")
    await SubprocessCommandRunner().run(
        (_RUNTIME_PYTHON, _RUNTIME_PROBE, "--model", model),
        environment=runtime_environment,
        timeout=120,
    )
    store = SQLiteStateStore(state_path)
    supervisor = RunSupervisor(
        store,
        client_factory=lambda execution: OpenAICodexClient(
            environment=runtime_environment,
            isolated_environment=True,
        ),
        model=model,
    )
    return _ContainedCodexDriver(
        supervisor,
        store,
        model=model,
        dependency_venv=dependency_venv,
        dependency_profile_id=dependency_profile_id,
        dependency_python_root=dependency_python_root,
        worker_owner=worker_owner,
        native_codex_home=Path(runtime_environment["CODEX_HOME"]),
    )
