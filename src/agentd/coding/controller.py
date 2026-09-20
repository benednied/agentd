"""Administrative composition for an allowlisted issue-to-draft controller."""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
from dataclasses import dataclass
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
)
from agentd.harness.registry import DriverRegistry
from agentd.intake.github import GitHubIssueSource
from agentd.intake.models import IntakePolicy, SourceIssue
from agentd.intake.service import GitHubIntake
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

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
            self.store.close()


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
    store = SQLiteStateStore(config["database"])
    try:
        intake = create_intake(config, store)
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
        runner = (
            MacOSSandboxValidationRunner()
            if platform.system() == "Darwin"
            else BubblewrapValidationRunner()
        )
        publications = CodingPublicationReconciler(
            store,
            DraftPublisher(
                PublicationStore(store.path),
                GitHubPublicationAdapter(),
                TrustedFinalizer(runner),
            ),
            {profile.id: profile},
            {profile.repository: Path(config["object_cache"])},
            {profile.repository: config["base_branch"]},
            source_refresh=intake.refresh_authorization,
        )

        last_reports: dict[str, str] = {}

        async def publish() -> None:
            if config.get("auto_resume_checkpoints", True):
                snapshot = store.latest_provider_quota_snapshot(config["account_pool"])
                policy = AccountPolicyThresholds(
                    background_block_used_percent=config.get(
                        "background_block_used_percent", 75
                    ),
                    urgent_only_used_percent=config.get("urgent_only_used_percent", 90),
                )
                if unattended_provider_wait_reason(snapshot, policy=policy) is None:
                    for job in store.list_jobs(frozenset({JobState.SUSPENDED})):
                        if store.latest_checkpoint(job.id) is None:
                            continue
                        try:
                            source = store.github_source_for_job(job.id)
                            if source is None:
                                continue
                            await asyncio.to_thread(
                                intake.refresh_authorization,
                                SourceIssue.from_dict(json.loads(source["payload"])),
                            )
                            await coordinator.resume(job.id)
                        except (LifecycleError, ValueError):
                            # Resetting the provider account does not authorize
                            # more cumulative job budget or changed source intent.
                            continue
            for result in await publications.reconcile():
                key = str(result.get("job_id") or result.get("url"))
                report = json.dumps(result, sort_keys=True)
                if last_reports.get(key) != report:
                    print(report, flush=True)
                    last_reports[key] = report
            for item in status(store):
                item["quota_wait_reason"] = coordinator.quota_wait_reason(
                    item["job_id"]
                )
                key = "status:" + item["job_id"]
                report = json.dumps(item, sort_keys=True)
                if last_reports.get(key) != report:
                    print(report, flush=True)
                    last_reports[key] = report

        daemon = AgentDaemon(
            plane,
            poll_interval=config.get("poll_interval_seconds", 30),
            account_oracle=AdministrativeOracle(
                config["quota_command"], store, config["account_pool"]
            ),
            account_poll_seconds=30,
            source_reconciler=intake.poll,
            result_reconciler=publish,
        )
        return CodingController(
            store, client, intake, publications, daemon, coordinator
        )
    except BaseException:
        store.close()
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
        result.append(
            {
                "job_id": job.id,
                "state": job.state.value,
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
