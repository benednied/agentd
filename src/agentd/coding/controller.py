"""Administrative composition for an allowlisted issue-to-draft controller."""

from __future__ import annotations

import asyncio
import json
import platform
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentd.coding.attempts import (
    CodingAttemptLimits,
    current_attempts,
    current_consumed,
)
from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.descriptor import RemoteCodingDescriptor
from agentd.coding.integration import GitHubIntegrationReconciler, integration_status
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler, CodingRepairReconciler
from agentd.coordinator import LifecycleError, SchedulerCoordinator
from agentd.daemon import AgentDaemon
from agentd.domain.enums import JobState, QuotaUnit, RunState
from agentd.domain.models import (
    CodingOperation,
    EffortEstimate,
    ProviderQuotaSnapshot,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    WorkerNode,
    utc_now,
)
from agentd.harness.registry import DriverRegistry
from agentd.intake.github import GitHubIssueSource
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.service import GitHubIntake
from agentd.intake.workflow import (
    GitHubStatusAdapter,
    GitHubStatusReporter,
    GitHubWorkflow,
    GitHubWorkflowSource,
    StandingGitHubPolicy,
)
from agentd.lifecycle import ControllerLock
from agentd.publication import (
    BubblewrapValidationRunner,
    DraftPublisher,
    GitHubPublicationAdapter,
    MacOSSandboxValidationRunner,
    PublicationStore,
    TrustedFinalizer,
)
from agentd.runtime.accounts import (
    DEFAULT_ACCOUNT_POLICY,
    AccountPolicyThresholds,
    JobUsagePolicy,
    unattended_provider_wait_reason,
)
from agentd.runtime.allowance import CheckpointBudgetPolicy, LocalAllowancePolicy
from agentd.runtime.governor import ProviderStopPolicy
from agentd.runtime.health import (
    RuntimeHealthStore,
    runtime_liveness,
    worker_heartbeat_ready,
)
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.client import RemoteWorkerClient
from agentd.workers.controller import RemoteWorkerEndpoint
from agentd.workers.registry import BackendRegistry
from agentd.workers.remote import RemoteWorkerBackend
from agentd.workspaces.git import GitWorkspaceManager


def load_config(path: Path) -> dict[str, Any]:
    """Resolve administrative file references relative to the configuration."""
    path = path.expanduser().resolve()
    config = json.loads(path.read_text())

    def resolve(value: str) -> str:
        candidate = Path(value).expanduser()
        # Do not resolve a final symlink: the transport rejects symlink PSKs.
        return str(candidate if candidate.is_absolute() else path.parent / candidate)

    for key in ("database", "object_cache", "unused_workspace_root"):
        config[key] = resolve(config[key])
    config["drain_file"] = resolve(
        config.get("drain_file", config["database"] + ".drain")
    )
    config["validation_runtime_mounts"] = [
        resolve(path) for path in config.get("validation_runtime_mounts", ())
    ]
    for key in ("psk_file", "tls_ca", "tls_client_cert", "tls_client_key"):
        if config["worker"].get(key):
            config["worker"][key] = resolve(config["worker"][key])
    return config


def coding_provider_policies(
    config: dict[str, Any],
) -> tuple[AccountPolicyThresholds, ProviderStopPolicy]:
    """Apply one reserve to admission, continuation and both provider windows.

    Percentages remain provider percentages; the local token allowance is a
    separate bound. Existing deployments keep their policy until opting in.
    """
    reserve = config.get("provider_reserve_percent")
    if reserve is None:
        return (
            AccountPolicyThresholds(
                background_block_used_percent=config.get(
                    "background_block_used_percent", 75
                ),
                urgent_only_used_percent=config.get("urgent_only_used_percent", 90),
            ),
            ProviderStopPolicy(),
        )
    if isinstance(reserve, bool) or not isinstance(reserve, int | float):
        raise ValueError("provider_reserve_percent must be a number")
    stop = ProviderStopPolicy(reserve / 100)
    ceiling = 100 - reserve
    return (
        AccountPolicyThresholds(
            background_block_used_percent=ceiling,
            urgent_only_used_percent=ceiling,
            require_complete_windows=True,
        ),
        stop,
    )


def coding_attempt_limits(config: dict[str, Any]) -> CodingAttemptLimits:
    """Use the same bounded attempt policy for repairs, resumes and reporting."""
    return CodingAttemptLimits(
        maximum_coding_attempts=config.get("maximum_automatic_attempts", 3),
        maximum_preparation_attempts=config.get("maximum_preparation_attempts", 3),
        maximum_total_attempts=config.get("maximum_total_attempts"),
    )


def guard_coding_resume(
    store: SQLiteStateStore, job_id: str, attempt_limits: CodingAttemptLimits
) -> None:
    """Keep GitHub resume controls within the same retained attempt limits."""
    reason = attempt_limits.blocked_reason(
        current_attempts(store.get_job(job_id), store.list_runs(job_id))
    )
    if reason is None:
        return
    run = store.latest_run(job_id)
    if run is not None:
        PublicationStore(store.path).record_repair(job_id, run.id, "exhausted", reason)
    raise LifecycleError(reason)


class AdministrativeOracle:
    """A bounded trusted adapter; command output must identify the correct pool."""

    def __init__(
        self, command: list[str], store: SQLiteStateStore, pool_id: str
    ) -> None:
        if not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError("quota_command requires nonempty argv")
        self.command, self.store, self.pool_id = command, store, pool_id

    async def snapshot(self) -> ProviderQuotaSnapshot:
        result = await asyncio.to_thread(
            subprocess.run,
            self.command,
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )
        snapshot = ProviderQuotaSnapshot.from_dict(json.loads(result.stdout))
        if snapshot.pool_id != self.pool_id:
            raise ValueError("quota observation belongs to a different account pool")
        self.store.append_provider_quota_snapshot(snapshot)
        return snapshot


def create_intake(config: dict[str, Any], store: SQLiteStateStore) -> GitHubIntake:
    profile = RepositoryProfile.from_dict(config["profile"])
    if not profile.validation_commands:
        raise ValueError("coding requires at least one trusted validation command")
    compiler = CodingJobCompiler(
        profile,
        config["base_commit"],
        QuotaBudget(
            config["expected_tokens"],
            maximum=config["maximum_tokens"],
            pool_id=config["account_pool"],
            unit=QuotaUnit.TOKENS,
        ),
        EffortEstimate(
            config.get("effort_p50_minutes", profile.max_runtime_seconds / 120),
            config.get("effort_p90_minutes", profile.max_runtime_seconds / 60),
        ),
        acceptance_criteria=tuple(config.get("acceptance_criteria", ())),
        quota_basis=config.get("token_quota_basis", "total-v1"),
        maximum_run_quota=config.get("maximum_run_tokens"),
    )
    return GitHubIntake(
        store,
        GitHubWorkflowSource()
        if config.get("standing_github_policy")
        else GitHubIssueSource(),
        (
            IntakePolicy(
                profile.repository,
                config["repository_id"],
                config.get("eligibility_label", "agentd:approved"),
            ),
        ),
        compiler,
        ControlPlane(store),
    )


@dataclass
class CodingController:
    store: SQLiteStateStore
    client: RemoteWorkerClient
    intake: GitHubIntake
    publications: CodingPublicationReconciler
    daemon: AgentDaemon
    coordinator: SchedulerCoordinator
    owner: ControllerLock
    backlog: Any = None
    runtime_health: RuntimeHealthStore | None = None
    workflow: GitHubWorkflow | None = None

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
            if self.runtime_health is not None:
                self.runtime_health.close()
            self.store.close()
            self.owner.release()


def create_backlog(config: dict[str, Any], intake: GitHubIntake) -> Any:
    from agentd.intake.backlog import GitHubAPITransport, GitHubBacklogSource
    from agentd.intake.backlog_service import BacklogReconciler
    from agentd.intake.integration import GitHubIntegrationSource

    return BacklogReconciler(
        intake,
        GitHubBacklogSource(GitHubAPITransport()),
        GitHubIntegrationSource(),
        config,
    )


def create_publications(
    config: dict[str, Any],
    store: SQLiteStateStore,
    intake: GitHubIntake,
    backlog: Any = None,
) -> CodingPublicationReconciler:
    profile = RepositoryProfile.from_dict(config["profile"])
    mounts = tuple(Path(path) for path in config.get("validation_runtime_mounts", ()))
    runner_type = (
        MacOSSandboxValidationRunner
        if platform.system() == "Darwin"
        else BubblewrapValidationRunner
    )
    runner = runner_type(runtime_mounts=mounts) if mounts else runner_type()
    return CodingPublicationReconciler(
        store,
        DraftPublisher(
            PublicationStore(store.path),
            GitHubPublicationAdapter(),
            TrustedFinalizer(runner),
            maximum_validation_attempts=config.get("maximum_validation_attempts", 3),
            allow_ready_pr_updates=config.get("allow_ready_pr_updates", False),
        ),
        {profile.id: profile},
        {profile.repository: Path(config["object_cache"])},
        {profile.repository: config["base_branch"]},
        source_refresh=(backlog or intake).refresh_authorization,
    )


def _published_subjects(store: SQLiteStateStore, repository: str) -> dict[int, str]:
    """Map only recorded publication URLs back to their logical source job."""
    ledger = PublicationStore(store.path)
    result = {}
    for job in store.list_jobs():
        row = ledger.get(job.id)
        if row is None or row.get("delivery") is None:
            continue
        url = row["delivery"]["pr_url"]
        if row["intent"]["repository"] != repository:
            continue
        prefix = f"https://github.com/{repository}/pull/"
        if url.startswith(prefix) and url[len(prefix) :].isdecimal():
            result[int(url[len(prefix) :])] = job.id
    return result


async def serve_publisher(config: dict[str, Any]) -> None:
    """Trusted publication process with no worker transport or dispatch authority."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    owner = ControllerLock(Path(config["database"] + ".publisher").resolve())
    with owner, SQLiteStateStore(config["database"]) as store:
        attempt_limits = coding_attempt_limits(config)
        intake = create_intake(config, store)
        backlog = create_backlog(config, intake) if config.get("backlog") else None
        publications = create_publications(config, store, intake, backlog)
        reporter = GitHubStatusReporter(
            store, GitHubStatusAdapter(), repository=config["profile"]["repository"]
        )
        integrations = (
            GitHubIntegrationReconciler(
                store,
                publications,
                config,
                source_refresh=(backlog or intake).refresh_authorization,
            )
            if config.get("integration", {}).get("enabled")
            else None
        )
        runtime_health = RuntimeHealthStore(config["database"])
        for received in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(received, stop.set)
        last_integration_poll = 0.0
        try:
            while not stop.is_set():
                runtime_health.pulse("publisher", state="validating")
                for result in await publications.reconcile():
                    key = str(result.get("job_id") or result.get("url"))
                    payload = json.dumps(result, sort_keys=True)
                    if store.changed_report("publication:" + key, result):
                        print(payload, flush=True)
                if (
                    integrations is not None
                    and time.monotonic() - last_integration_poll
                    >= config.get("integration_poll_seconds", 60)
                ):
                    for result in await integrations.reconcile():
                        print(json.dumps(result, sort_keys=True), flush=True)
                    last_integration_poll = time.monotonic()
                if config.get("standing_github_policy"):
                    reporter.enqueue(
                        status(
                            store,
                            repository=config["profile"]["repository"],
                            attempt_limits=attempt_limits,
                            account_policy=coding_provider_policies(config)[0],
                        )
                    )
                    await reporter.publish_pending()
                runtime_health.pulse("publisher")
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=config.get("poll_interval_seconds", 30)
                    )
        finally:
            runtime_health.close()
            for received in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(received)


def create_controller(config: dict[str, Any]) -> CodingController:
    """Reuse the existing scheduler, ownership ledger, and trusted publisher."""
    attempt_limits = coding_attempt_limits(config)
    profile = RepositoryProfile.from_dict(config["profile"])
    endpoint_data = dict(config["worker"])
    for key in ("psk_file", "tls_ca", "tls_client_cert", "tls_client_key"):
        if endpoint_data.get(key):
            endpoint_data[key] = Path(endpoint_data[key])
    endpoint_data["features"] = frozenset(endpoint_data.get("features", ()))
    endpoint = RemoteWorkerEndpoint(**endpoint_data)
    # File references only; no ambient transport secret is passed to subprocesses.
    client = endpoint._create_client(endpoint.load_secret({}))
    owner = ControllerLock(Path(config["database"]).expanduser().resolve())
    owner.acquire()
    try:
        store = SQLiteStateStore(config["database"])
    except BaseException:
        owner.release()
        raise
    runtime_health = None
    try:
        intake = create_intake(config, store)
        backlog = create_backlog(config, intake) if config.get("backlog") else None
        backend = RemoteWorkerBackend(
            client,
            name=endpoint.name,
            node_id=endpoint.node_id,
            expected_driver="remote-coding",
        )
        coordinator = SchedulerCoordinator(
            store,
            GitWorkspaceManager(config["unused_workspace_root"]),
            DriverRegistry(
                [
                    RemoteCodingDescriptor(
                        frozenset(
                            {
                                "remote-coding",
                                "harness-codex",
                                f"repository-profile-{profile.digest}",
                            }
                        )
                        | frozenset(profile.required_capabilities)
                    )
                ]
            ),
            backends=BackendRegistry([backend]),
            account_policy=coding_provider_policies(config)[0],
            provider_stop_policy=coding_provider_policies(config)[1],
            usage_policy=JobUsagePolicy(
                top_up_at_fraction=config.get("top_up_at_fraction", 0.8),
                checkpoint_at_fraction=config.get("checkpoint_at_fraction", 0.9),
                hard_cap_at_fraction=1.0,
            ),
        )
        plane = ControlPlane(store, coordinator=coordinator)
        intake.control_plane = plane
        plane.register_node(
            WorkerNode(
                endpoint.node_id,
                labels={"backend": endpoint.name},
                capacity=ResourceVector(1, 1),
                harnesses=frozenset({"remote-coding"}),
            )
        )
        plane.register_quota_pool(
            QuotaPool(
                config["account_pool"],
                "codex",
                config["maximum_tokens"],
                unit=QuotaUnit.TOKENS,
                minimum_interactive_reserve=config.get(
                    "minimum_interactive_reserve", 0
                ),
            )
        )
        publications = create_publications(config, store, intake, backlog)
        runtime_health = RuntimeHealthStore(config["database"])
        reporter = GitHubStatusReporter(
            store, GitHubStatusAdapter(), repository=profile.repository
        )
        repairs = CodingRepairReconciler(
            store,
            publications,
            coordinator,
            maximum_attempts=attempt_limits.maximum_coding_attempts,
            maximum_preparation_attempts=attempt_limits.maximum_preparation_attempts,
            maximum_total_attempts=attempt_limits.maximum_total_attempts,
        )
        integrations = (
            GitHubIntegrationReconciler(
                store,
                publications,
                config,
                source_refresh=(backlog or intake).refresh_authorization,
            )
            if config.get("integration", {}).get("enabled")
            else None
        )
        workflow = None
        if config.get("standing_github_policy"):
            policy_config = {
                "repository": profile.repository,
                "repository_id": config["repository_id"],
                **config["standing_github_policy"],
            }
            workflow = GitHubWorkflow(
                intake,
                intake.source,
                StandingGitHubPolicy.from_dict(policy_config),
                feedback=repairs.request_feedback,
                resume_guard=lambda job_id: guard_coding_resume(
                    store, job_id, attempt_limits
                ),
                approve_pr=integrations.approve_pr if integrations else None,
                published_subjects=lambda: _published_subjects(
                    store, profile.repository
                ),
            )
            intake.workflow = workflow
        budget_policy = (
            CheckpointBudgetPolicy(**config["automatic_resume_budget"])
            if config.get("automatic_resume_budget")
            else None
        )

        from agentd.coding.admin import CodingAdminStore

        admin_requests = CodingAdminStore(config["database"])

        async def publish() -> None:
            runtime_health.pulse("controller")
            await admin_requests.apply_pending(coordinator)
            if (
                config.get("auto_resume_checkpoints", True)
                and not Path(
                    config.get("drain_file", config["database"] + ".drain")
                ).exists()
            ):
                snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
                policy = coding_provider_policies(config)[0]
                if unattended_provider_wait_reason(snapshot, policy=policy) is None:
                    for job in store.list_jobs(frozenset({JobState.SUSPENDED})):
                        if (
                            not isinstance(job.operation, CodingOperation)
                            or job.operation.work_order.repository != profile.repository
                        ):
                            continue
                        if store.github_job_held(job.id):
                            continue
                        if attempt_limits.blocked_reason(
                            current_attempts(job, store.list_runs(job.id))
                        ):
                            continue
                        if store.latest_checkpoint(job.id) is None:
                            continue
                        try:
                            source = store.github_source_for_job(job.id)
                            if source is None:
                                continue
                            await asyncio.to_thread(
                                (backlog or intake).refresh_authorization,
                                SourceIssue.from_dict(json.loads(source["payload"])),
                            )
                            consumed = current_consumed(
                                job, store.list_reservations(job.id)
                            )
                            maximum = (
                                budget_policy.replanned_maximum(
                                    job.quota_budget.maximum, consumed
                                )
                                if budget_policy
                                else None
                            )
                            await coordinator.resume(
                                job.id,
                                maximum_tokens=maximum,
                                actor="standing-checkpoint-budget-policy"
                                if maximum is not None
                                else None,
                            )
                        except (LifecycleError, ValueError):
                            # Resetting the provider account does not authorize
                            # more cumulative job budget or changed source intent.
                            continue
            results = (
                await publications.reconcile()
                if config.get("publication_enabled", True)
                else ()
            )
            for result in results:
                key = str(result.get("job_id") or result.get("url"))
                report = json.dumps(result, sort_keys=True)
                if store.changed_report("publication:" + key, result):
                    print(report, flush=True)
            if config.get("automatic_validation_repair", bool(workflow)):
                await repairs.reconcile()
            if workflow is not None:
                await workflow.apply_pending()
                reporter.enqueue(
                    status(
                        store,
                        repository=profile.repository,
                        attempt_limits=attempt_limits,
                        account_policy=coding_provider_policies(config)[0],
                    )
                )
            for item in status(
                store,
                repository=profile.repository,
                attempt_limits=attempt_limits,
                account_policy=coding_provider_policies(config)[0],
            ):
                item["quota_wait_reason"] = coordinator.quota_wait_reason(
                    item["job_id"]
                )
                key = "status:" + item["job_id"]
                report = json.dumps(item, sort_keys=True)
                if store.changed_report(key, item):
                    print(report, flush=True)
            snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
            runtime_health.pulse(
                "controller",
                state="source_unavailable"
                if daemon.source_refresh_failed
                else "waiting_quota"
                if unattended_provider_wait_reason(
                    snapshot, policy=coding_provider_policies(config)[0]
                )
                is not None
                else "polling",
            )

        async def refresh_sources() -> None:
            if config.get("refresh_base_from_github") and backlog is None:
                from agentd.intake.integration import GitHubIntegrationSource

                base = await asyncio.to_thread(
                    GitHubIntegrationSource().target_commit,
                    profile.repository,
                    config["base_branch"],
                )
                intake.compile_job = replace(intake.compile_job, base_commit=base)
            await (backlog or intake).poll()
            runtime_health.pulse("source")

        async def report_recovery(error: Exception) -> None:
            del error
            runtime_health.pulse("controller", state="ownership_blocked")
            if workflow is not None:
                reports = status(
                    store,
                    repository=profile.repository,
                    attempt_limits=attempt_limits,
                    account_policy=coding_provider_policies(config)[0],
                )
                for report in reports:
                    report["blocked_reason"] = "Execution ownership is unresolved"
                reporter.enqueue(reports)

        def report_error(error: Exception) -> None:
            runtime_health.pulse("controller", state="retrying")
            if workflow is not None:
                reports = status(
                    store,
                    repository=profile.repository,
                    attempt_limits=attempt_limits,
                    account_policy=coding_provider_policies(config)[0],
                )
                for report in reports:
                    report["blocked_reason"] = (
                        "Controller is retrying after " + type(error).__name__
                    )
                reporter.enqueue(reports)

        daemon = AgentDaemon(
            plane,
            poll_interval=config.get("poll_interval_seconds", 30),
            account_oracle=AdministrativeOracle(
                config["quota_command"], store, config["account_pool"]
            ),
            account_poll_seconds=30,
            source_reconciler=refresh_sources,
            source_poll_seconds=config.get(
                "source_poll_seconds", 60 if backlog else None
            ),
            result_reconciler=publish,
            admission_enabled=lambda: (
                not Path(
                    config.get("drain_file", config["database"] + ".drain")
                ).exists()
            ),
            local_allowance_policy=LocalAllowancePolicy.from_config(
                config["local_allowance"], pool_id=config["account_pool"]
            )
            if config.get("local_allowance")
            else None,
            recovery_reporter=report_recovery,
            on_error=report_error,
        )
        return CodingController(
            store,
            client,
            intake,
            publications,
            daemon,
            coordinator,
            owner,
            backlog,
            runtime_health,
            workflow,
        )
    except BaseException:
        if runtime_health is not None:
            runtime_health.close()
        store.close()
        owner.release()
        raise


def status(
    store: SQLiteStateStore,
    *,
    repository: str | None = None,
    attempt_limits: CodingAttemptLimits | None = None,
    account_policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> list[dict[str, Any]]:
    """Safe identities and outcomes, without raw issue text or credentials."""
    result = []
    snapshots: dict[str, ProviderQuotaSnapshot | None] = {}
    limits = attempt_limits or CodingAttemptLimits()
    publication_store = PublicationStore(store.path)
    with RuntimeHealthStore(store.path) as health_store:
        controller_pulse = health_store.latest("controller")
    runtime_reason = {
        "source_unavailable": (
            "GitHub source polling is unavailable; retrying automatically"
        ),
        "ownership_blocked": (
            "Execution ownership or final usage is unresolved; "
            "retaining the reservation"
        ),
    }.get(controller_pulse.state if controller_pulse else None)
    for job in store.list_jobs():
        source = store.github_source_for_job(job.id)
        if source is None:
            continue
        if (
            repository is not None
            and json.loads(source["payload"])["repository"] != repository
        ):
            continue
        run = store.latest_run(job.id)
        runs = current_attempts(job, store.list_runs(job.id))
        counts = limits.count(runs)
        repair = publication_store.repair_for(job.id, run.id) if run else None
        if job.state is JobState.SUSPENDED:
            attempt_reason = limits.blocked_reason(runs)
            if attempt_reason:
                repair = {"status": "exhausted", "reason": attempt_reason}
        publication = publication_store.get(job.id)
        preflight = publication_store.preflight_for(job.id, run.id) if run else None
        gate = store.backlog_gate(job.id)
        integration = integration_status(store, job.id)
        try:
            pool = store.get_quota_pool(job.quota_budget.pool_id)
            local_capacity = pool.remaining - pool.reserved
        except LookupError:
            local_capacity = None
        pool_id = job.quota_budget.pool_id
        if pool_id not in snapshots:
            snapshots[pool_id] = store.latest_provider_quota_snapshot(pool_id)
        snapshot = snapshots[pool_id]
        result.append(
            {
                "job_id": job.id,
                "state": job.state.value,
                "issue_url": (
                    f"https://github.com/{json.loads(source['payload'])['repository']}"
                    f"/issues/{json.loads(source['payload'])['number']}"
                ),
                "last_progress_at": job.updated_at.isoformat(),
                "blocked_reason": (
                    "Physical ownership retired through GitHub; final usage "
                    "remains unknown and its reservation is retained. "
                    "New issues can proceed within the remaining allowance."
                    if run and run.state is RunState.QUARANTINED
                    else preflight["reason"] + "; retrying publication automatically"
                    if preflight
                    else runtime_reason
                )
                if job.state not in {JobState.COMPLETED, JobState.CANCELLED}
                else None,
                "attempts": counts.total,
                "historical_attempts": len(store.list_runs(job.id)),
                "attempt_budget": {
                    "coding_attempts": counts.coding,
                    "preparation_attempts": counts.preparation,
                    "total_attempts": counts.total,
                    "maximum_coding_attempts": limits.maximum_coding_attempts,
                    "maximum_preparation_attempts": limits.maximum_preparation_attempts,
                    "maximum_total_attempts": limits.maximum_total_attempts,
                },
                "backlog_wait_reason": gate["reason"]
                if gate and not gate["ready"]
                else None,
                "quota": {
                    "unit": job.quota_budget.unit.value,
                    "local_available": local_capacity,
                    "job_maximum": job.quota_budget.maximum,
                    "run_maximum": job.operation.work_order.maximum_run_quota
                    if isinstance(job.operation, CodingOperation)
                    else None,
                    "job_consumed": current_consumed(
                        job, store.list_reservations(job.id)
                    ),
                    "quota_basis": job.operation.work_order.quota_basis
                    if isinstance(job.operation, CodingOperation)
                    else "total-v1",
                    "historical_consumed": sum(
                        r.consumed for r in store.list_reservations(job.id)
                    ),
                    "provider_wait_reason": unattended_provider_wait_reason(
                        snapshot, policy=account_policy
                    ),
                    "provider_observed_at": snapshot.observed_at.isoformat()
                    if snapshot
                    else None,
                    "provider_reset_refills_local_budget": False,
                },
                "held": store.github_job_held(job.id),
                "authorized": bool(
                    not store.github_job_held(job.id)
                    and not source["revoked"]
                    and source["eligible"]
                    and source["approved_revision"] == source["revision"]
                ),
                "run_id": run.id if run else None,
                "run_outcome": run.result.outcome.value if run and run.result else None,
                "publication_stage": publication["stage"] if publication else None,
                "pr": (publication.get("delivery") or {}).get("pr_url")
                if publication and publication.get("delivery")
                else publication["pr"].get("url")
                if publication and publication["pr"]
                else None,
                "integration_stage": integration.get("stage") if integration else None,
                "integration_wait_reason": integration.get("reason")
                if integration
                else None,
                "merge_commit": integration.get("merge_commit")
                if integration
                else None,
                "repair": repair,
                "delivery": publication.get("delivery") if publication else None,
            }
        )
    return result


def health(config: dict[str, Any], store: SQLiteStateStore) -> dict[str, Any]:
    """Read telemetry readiness without SSH, model calls, or worker credentials."""
    now = utc_now()
    controller_owner = ControllerLock(Path(config["database"]).resolve()).held()
    publisher_owner = ControllerLock(
        Path(config["database"] + ".publisher").resolve()
    ).held()
    with RuntimeHealthStore(config["database"]) as pulse_store:
        controller_pulse = pulse_store.latest("controller")
        publisher_pulse = pulse_store.latest("publisher")
        source_pulse = pulse_store.latest("source")
    profile = RepositoryProfile.from_dict(config["profile"])
    liveness = {
        "controller": runtime_liveness(
            controller_pulse, owner_live=controller_owner, at=now
        ),
        "publisher": runtime_liveness(
            publisher_pulse,
            owner_live=publisher_owner,
            at=now,
            stale_after_seconds=max(
                180,
                profile.validation_timeout_seconds * len(profile.validation_commands)
                + 120,
            ),
        ),
    }
    controller_live = (
        liveness["controller"]["live"]
        if config.get("standing_github_policy")
        else controller_owner
    )
    draining = Path(config.get("drain_file", config["database"] + ".drain")).exists()
    snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
    policy = coding_provider_policies(config)[0]
    provider_reason = unattended_provider_wait_reason(snapshot, policy=policy)
    workers = [
        {
            "node_id": node.id,
            "fresh": worker_heartbeat_ready(
                node.heartbeat,
                at=now,
                expected_epoch=config["worker"].get("session_epoch"),
            ),
            "active_runs": node.heartbeat.active_runs if node.heartbeat else None,
        }
        for node in store.list_nodes()
        if node.id == config["worker"]["node_id"]
    ]
    backlog_status = None
    source_fresh = not config.get("backlog")
    if config.get("standing_github_policy"):
        source_fresh = bool(
            source_pulse
            and timedelta(0) <= now - source_pulse.observed_at <= timedelta(seconds=180)
        )
    if config.get("backlog"):
        backlog = create_backlog(config, create_intake(config, store))
        backlog_status = backlog.ledger.status()
        source_fresh = bool(
            backlog_status
            and backlog_status.get("complete")
            and timedelta(0)
            <= now - datetime.fromisoformat(backlog_status["observed_at"])
            <= timedelta(seconds=120)
        )
    ready = bool(
        controller_live
        and not draining
        and workers
        and all(w["fresh"] for w in workers)
        and source_fresh
        and provider_reason is None
    )
    return {
        "controller_live": controller_live,
        "liveness": liveness,
        "draining": draining,
        "unresolved_runs": [
            run.id
            for run in store.list_runs()
            if run.state.value in {"STARTING", "RUNNING", "DRAINING", "CHECKPOINTED"}
        ],
        "admission_telemetry_ready": ready,
        "workers": workers,
        "provider_wait_reason": provider_reason,
        "source_fresh": source_fresh,
        "backlog": backlog_status,
    }
