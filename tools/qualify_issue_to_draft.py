#!/usr/bin/env python3
"""Bounded internal composition for the real issue-to-draft qualification.

The JSON configuration is trusted local administrative policy, never issue text.
It references an existing verified worker and never deploys infrastructure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from agentd.coding.compiler import CodingJobCompiler
from agentd.coding.descriptor import RemoteCodingDescriptor
from agentd.coding.models import RepositoryProfile
from agentd.coding.pipeline import CodingPublicationReconciler
from agentd.coordinator import SchedulerCoordinator
from agentd.daemon import AgentDaemon
from agentd.domain.enums import QuotaUnit
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
from agentd.intake.models import IntakePolicy
from agentd.intake.service import GitHubIntake
from agentd.publication import (
    DraftPublisher,
    GitHubPublicationAdapter,
    MacOSSandboxValidationRunner,
    PublicationStore,
    TrustedFinalizer,
)
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.controller import RemoteWorkerEndpoint
from agentd.workers.registry import BackendRegistry
from agentd.workers.remote import RemoteWorkerBackend
from agentd.workspaces.git import GitWorkspaceManager


class AdministrativeOracle:
    """Read provider observations through a configured, bounded local adapter."""

    def __init__(self, command: list[str], store: SQLiteStateStore) -> None:
        self.command, self.store = command, store

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
        self.store.append_provider_quota_snapshot(snapshot)
        return snapshot


async def qualify(config: dict[str, Any], approve_as: str | None) -> dict[str, Any]:
    profile = RepositoryProfile.from_dict(config["profile"])
    endpoint_data = dict(config["worker"])
    for key in ("psk_file", "tls_ca", "tls_client_cert", "tls_client_key"):
        if endpoint_data.get(key):
            endpoint_data[key] = Path(endpoint_data[key])
    endpoint_data["features"] = frozenset(endpoint_data.get("features", ()))
    endpoint = RemoteWorkerEndpoint(**endpoint_data)
    client = endpoint._create_client(endpoint.load_secret({}))
    backend = RemoteWorkerBackend(
        client,
        name=endpoint.name,
        node_id=endpoint.node_id,
        expected_driver="remote-coding",
    )
    store = SQLiteStateStore(config["database"])
    features = frozenset(
        {"remote-coding", "harness-codex", f"repository-profile-{profile.digest}"}
    )
    descriptor = RemoteCodingDescriptor(features)
    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(config["unused_workspace_root"]),
        DriverRegistry([descriptor]),
        backends=BackendRegistry([backend]),
    )
    plane = ControlPlane(store, coordinator=coordinator)
    plane.register_node(
        WorkerNode(
            endpoint.node_id,
            labels={"backend": endpoint.name},
            capacity=ResourceVector(1, 1),
            harnesses=frozenset({"remote-coding"}),
        )
    )
    pool = config["account_pool"]
    plane.register_quota_pool(
        QuotaPool(pool, "codex", config["maximum_tokens"], unit=QuotaUnit.TOKENS)
    )
    compiler = CodingJobCompiler(
        profile,
        config["base_commit"],
        QuotaBudget(
            config["expected_tokens"],
            maximum=config["maximum_tokens"],
            pool_id=pool,
            unit=QuotaUnit.TOKENS,
        ),
        EffortEstimate(3, 5),
    )
    source = GitHubIssueSource()
    intake = GitHubIntake(
        store,
        source,
        (IntakePolicy(profile.repository, config["repository_id"]),),
        compiler,
        plane,
    )
    if approve_as is not None:
        await asyncio.to_thread(
            intake.approve, profile.repository, config["issue_number"], actor=approve_as
        )
    issue = await asyncio.to_thread(
        source.get, profile.repository, config["issue_number"]
    )
    await intake.reconcile(issue)
    await intake.reconcile(issue)
    publisher_store = PublicationStore(config["database"])
    publisher = DraftPublisher(
        publisher_store,
        GitHubPublicationAdapter(),
        TrustedFinalizer(MacOSSandboxValidationRunner()),
    )
    publications = CodingPublicationReconciler(
        store,
        publisher,
        {profile.id: profile},
        {profile.repository: Path(config["object_cache"])},
        {profile.repository: config["base_branch"]},
    )

    async def refresh_source() -> None:
        current = await asyncio.to_thread(
            source.get, profile.repository, config["issue_number"]
        )
        await intake.reconcile(current)

    published: dict[str, Any] = {}

    async def publish() -> None:
        for result in await publications.reconcile():
            if result.get("url"):
                published.update(result)
            elif result.get("publication_error"):
                print(json.dumps(result), flush=True)

    daemon = AgentDaemon(
        plane,
        account_oracle=AdministrativeOracle(config["quota_command"], store),
        account_poll_seconds=30,
        source_reconciler=refresh_source,
        result_reconciler=publish,
    )
    started = time.monotonic()
    last_state = None
    try:
        await coordinator.recover_managed_runs()
        while time.monotonic() - started < config.get(
            "controller_timeout_seconds", 600
        ):
            await daemon.tick()
            job = store.get_job(issue.job_id)
            if job.state.value != last_state:
                last_state = job.state.value
                print(
                    json.dumps(
                        {
                            "job_id": job.id,
                            "state": last_state,
                            "wait_reason": coordinator.quota_wait_reason(job.id)
                            if last_state == "READY"
                            else None,
                        }
                    ),
                    flush=True,
                )
            if published or job.terminal:
                break
            await asyncio.sleep(2)
        run = store.latest_run(issue.job_id)
        evidence = {
            "issue": f"https://github.com/{profile.repository}/issues/{issue.number}",
            "source_revision": issue.revision,
            "job_id": issue.job_id,
            "job_count": len(store.list_jobs()),
            "run_count": len(store.list_runs()),
            "job_state": store.get_job(issue.job_id).state.value,
            "run_id": run.id if run else None,
            "worker_id": run.node_id if run else None,
            "base_commit": config["base_commit"],
            "result_commit": run.result.commit if run and run.result else None,
            "consumed_tokens": run.result.consumed_quota
            if run and run.result
            else None,
            "profile_digest": profile.digest,
            "pr": published.get("url"),
            "draft": published.get("isDraft"),
            "runtime_seconds": round(time.monotonic() - started, 2),
            "source_commit": config["source_commit"],
            "worker_source_commit": config["worker_source_commit"],
            "error_type": type(daemon.last_error).__name__
            if daemon.last_error
            else None,
        }
        Path(config["evidence_path"]).write_text(json.dumps(evidence, indent=2) + "\n")
        return evidence
    finally:
        await client.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--approve-as",
        help="Explicit trusted local approval for this exact issue revision",
    )
    args = parser.parse_args()
    evidence = asyncio.run(
        qualify(json.loads(args.config.read_text()), args.approve_as)
    )
    print(json.dumps(evidence, indent=2))
    if not evidence.get("pr"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
