"""Administrative composition for an allowlisted issue-to-draft controller."""

from __future__ import annotations

import asyncio
import json
import platform
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.descriptor import RemoteCodingDescriptor
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler
from agentd.coordinator import LifecycleError, SchedulerCoordinator
from agentd.daemon import AgentDaemon
from agentd.domain.enums import JobState, QuotaUnit
from agentd.domain.models import (
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
    AccountPolicyThresholds,
    JobUsagePolicy,
    unattended_provider_wait_reason,
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
    )
    return GitHubIntake(
        store,
        GitHubIssueSource(),
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

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
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
        ),
        {profile.id: profile},
        {profile.repository: Path(config["object_cache"])},
        {profile.repository: config["base_branch"]},
        source_refresh=(backlog or intake).refresh_authorization,
    )


async def serve_publisher(config: dict[str, Any]) -> None:
    """Trusted publication process with no worker transport or dispatch authority."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    owner = ControllerLock(Path(config["database"] + ".publisher").resolve())
    with owner, SQLiteStateStore(config["database"]) as store:
        intake = create_intake(config, store)
        backlog = create_backlog(config, intake) if config.get("backlog") else None
        publications = create_publications(config, store, intake, backlog)
        for received in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(received, stop.set)
        try:
            while not stop.is_set():
                for result in await publications.reconcile():
                    key = str(result.get("job_id") or result.get("url"))
                    payload = json.dumps(result, sort_keys=True)
                    if store.changed_report("publication:" + key, result):
                        print(payload, flush=True)
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=config.get("poll_interval_seconds", 30)
                    )
        finally:
            for received in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(received)


def create_controller(config: dict[str, Any]) -> CodingController:
    """Reuse the existing scheduler, ownership ledger, and trusted publisher."""
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
    store = SQLiteStateStore(config["database"])
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
            account_policy=AccountPolicyThresholds(
                background_block_used_percent=config.get(
                    "background_block_used_percent", 75
                ),
                urgent_only_used_percent=config.get("urgent_only_used_percent", 90),
            ),
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
            )
        )
        publications = create_publications(config, store, intake, backlog)

        async def publish() -> None:
            if (
                config.get("auto_resume_checkpoints", True)
                and not Path(
                    config.get("drain_file", config["database"] + ".drain")
                ).exists()
            ):
                snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
                policy = AccountPolicyThresholds(
                    background_block_used_percent=config.get(
                        "background_block_used_percent", 75
                    ),
                    urgent_only_used_percent=config.get("urgent_only_used_percent", 90),
                )
                if unattended_provider_wait_reason(snapshot, policy=policy) is None:
                    for job in store.list_jobs(frozenset({JobState.SUSPENDED})):
                        if len(store.list_runs(job.id)) >= config.get(
                            "maximum_automatic_attempts", 3
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
                            await coordinator.resume(job.id)
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
            for item in status(store):
                item["quota_wait_reason"] = coordinator.quota_wait_reason(
                    item["job_id"]
                )
                key = "status:" + item["job_id"]
                report = json.dumps(item, sort_keys=True)
                if store.changed_report(key, item):
                    print(report, flush=True)

        daemon = AgentDaemon(
            plane,
            poll_interval=config.get("poll_interval_seconds", 30),
            account_oracle=AdministrativeOracle(
                config["quota_command"], store, config["account_pool"]
            ),
            account_poll_seconds=30,
            source_reconciler=(backlog or intake).poll,
            result_reconciler=publish,
            admission_enabled=lambda: (
                not Path(
                    config.get("drain_file", config["database"] + ".drain")
                ).exists()
            ),
        )
        return CodingController(
            store, client, intake, publications, daemon, coordinator, owner, backlog
        )
    except BaseException:
        store.close()
        owner.release()
        raise


def status(store: SQLiteStateStore) -> list[dict[str, Any]]:
    """Safe identities and outcomes, without raw issue text or credentials."""
    result = []
    publication_store = PublicationStore(store.path)
    for job in store.list_jobs():
        source = store.github_source_for_job(job.id)
        if source is None:
            continue
        run = store.latest_run(job.id)
        publication = publication_store.get(job.id)
        gate = store.backlog_gate(job.id)
        try:
            pool = store.get_quota_pool(job.quota_budget.pool_id)
            local_capacity = pool.remaining - pool.reserved
        except LookupError:
            local_capacity = None
        snapshot = store.latest_provider_quota_snapshot(job.quota_budget.pool_id)
        result.append(
            {
                "job_id": job.id,
                "state": job.state.value,
                "issue_url": (
                    f"https://github.com/{json.loads(source['payload'])['repository']}"
                    f"/issues/{json.loads(source['payload'])['number']}"
                ),
                "last_progress_at": job.updated_at.isoformat(),
                "attempts": len(store.list_runs(job.id)),
                "backlog_wait_reason": gate["reason"]
                if gate and not gate["ready"]
                else None,
                "quota": {
                    "unit": job.quota_budget.unit.value,
                    "local_available": local_capacity,
                    "job_maximum": job.quota_budget.maximum,
                    "job_consumed": sum(
                        r.consumed for r in store.list_reservations(job.id)
                    ),
                    "provider_wait_reason": unattended_provider_wait_reason(snapshot),
                    "provider_observed_at": snapshot.observed_at.isoformat()
                    if snapshot
                    else None,
                    "provider_reset_refills_local_budget": False,
                },
                "authorized": bool(
                    not source["revoked"]
                    and source["eligible"]
                    and source["approved_revision"] == source["revision"]
                ),
                "run_id": run.id if run else None,
                "run_outcome": run.result.outcome.value if run and run.result else None,
                "publication_stage": publication["stage"] if publication else None,
                "pr": publication["pr"].get("url")
                if publication and publication["pr"]
                else None,
            }
        )
    return result


def health(config: dict[str, Any], store: SQLiteStateStore) -> dict[str, Any]:
    """Read telemetry readiness without SSH, model calls, or worker credentials."""
    now = utc_now()
    controller_live = ControllerLock(Path(config["database"]).resolve()).held()
    draining = Path(config.get("drain_file", config["database"] + ".drain")).exists()
    snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
    policy = AccountPolicyThresholds(
        background_block_used_percent=config.get("background_block_used_percent", 75),
        urgent_only_used_percent=config.get("urgent_only_used_percent", 90),
    )
    provider_reason = unattended_provider_wait_reason(snapshot, policy=policy)
    workers = [
        {
            "node_id": node.id,
            "fresh": bool(
                node.heartbeat
                and timedelta(0)
                <= now - node.heartbeat.observed_at
                <= timedelta(seconds=45)
            ),
            "active_runs": node.heartbeat.active_runs if node.heartbeat else None,
        }
        for node in store.list_nodes()
        if node.id == config["worker"]["node_id"]
    ]
    backlog_status = None
    source_fresh = not config.get("backlog")
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
