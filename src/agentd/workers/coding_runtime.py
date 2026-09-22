"""Compose the existing SDK/supervisor behind the deployed sandbox preflight.

This is a trusted worker bootstrap API, not a protocol operation. Passing a
boolean never grants containment features: the deployed runtime proof must exit
successfully under the same environment used to start the SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from agentd.coding.models import CodingWorkOrder
from agentd.domain.enums import JobState, QuotaUnit, RunOutcome
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
    RunRecord,
    RunResult,
    StateTransition,
    TokenUsage,
    WorkerNode,
    WorkspaceLease,
)
from agentd.harness.app_server import DEFAULT_CODEX_MODEL, OpenAICodexClient
from agentd.harness.codex_sdk import CodexSdkDriver
from agentd.harness.supervisor import RunSupervisor
from agentd.state.base import EntityNotFoundError
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.operations import OperationError, SubprocessCommandRunner

_RUNTIME_PYTHON = "/opt/agentd/venv/bin/python"
_RUNTIME_PROBE = "/opt/agentd/security/runtime_sandbox_probe.py"


class CodingPreparationError(OperationError):
    """The local execution envelope failed before any provider call."""


class _ContainedCodexDriver(CodexSdkDriver):
    """SDK with a worker-local execution-envelope ledger, never a quota oracle."""

    def __init__(
        self, supervisor: RunSupervisor, store: SQLiteStateStore, *, model: str
    ) -> None:
        super().__init__(supervisor, model=model)
        self._worker_store = store

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

    async def close(self) -> None:
        await super().close()
        self._worker_store.close()


async def create_verified_coding_sdk(
    state_path: Path,
    *,
    environment: Mapping[str, str],
    model: str = DEFAULT_CODEX_MODEL,
) -> CodexSdkDriver:
    """Run the image-pinned probe, then grant narrowly proven capabilities.

    Only the existing reviewed Linux container layout is supported. Deployment
    credentials stay in the dedicated auth file whose unreadability the probe
    actually tests. API-key or publication-token environment injection is not
    accepted. State paths must remain beneath the protected deployment state
    root, outside every coding workspace.
    """
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
        (_RUNTIME_PYTHON, _RUNTIME_PROBE),
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
    return _ContainedCodexDriver(supervisor, store, model=model)
