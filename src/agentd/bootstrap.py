"""Composition helpers for a local agentd process."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from agentd.config import ServiceConfig
from agentd.coordinator import SchedulerCoordinator
from agentd.domain.models import ExecutionContract
from agentd.harness.app_server import OpenAICodexClient
from agentd.harness.codex import CodexCliDriver
from agentd.harness.codex_sdk import CodexSdkDriver
from agentd.harness.fake import FakeHarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.harness.supervisor import RunSupervisor
from agentd.provisioning import TrustedUvProvisioner
from agentd.runtime.accounts import AccountPolicyThresholds, JobUsagePolicy
from agentd.runtime.codex_oracle import CodexAccountOracle
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.local import LocalWorkerBackend
from agentd.workers.registry import BackendRegistry
from agentd.workspaces.git import GitWorkspaceManager


@dataclass(slots=True)
class LocalRuntime:
    store: SQLiteStateStore
    coordinator: SchedulerCoordinator
    control_plane: ControlPlane
    drivers: DriverRegistry
    supervisor: RunSupervisor | None = None
    account_oracle: CodexAccountOracle | None = None

    async def aclose(self) -> None:
        """Quiesce driver transports before closing durable state."""

        if self.supervisor is not None:
            await self.supervisor.close()
        self.store.close()

    def close(self) -> None:
        """Close a runtime from synchronous embedding code."""

        if self.supervisor is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self.supervisor.close())
            else:
                raise RuntimeError("Use await runtime.aclose() inside an event loop")
        self.store.close()

    def __enter__(self) -> LocalRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def create_local_runtime(
    database: str | Path,
    workspace_root: str | Path,
    *,
    include_fake_driver: bool = True,
    include_codex_driver: bool = True,
    include_codex_cli_driver: bool = True,
    trusted_provisioning: bool = False,
    enforce_codex_account_policy: bool = False,
    config: ServiceConfig | None = None,
) -> LocalRuntime:
    """Compose the local SQLite/Git/process implementation behind domain ports."""

    database_path = Path(database)
    if str(database) != ":memory:":
        database_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    effective_config = config or ServiceConfig.from_environment(
        {
            **os.environ,
            "AGENTD_DB": str(database),
            "AGENTD_WORKSPACE_ROOT": str(workspace_root),
        }
    )
    store = SQLiteStateStore(database)
    drivers = DriverRegistry()
    supervisor: RunSupervisor | None = None
    account_oracle: CodexAccountOracle | None = None
    if include_fake_driver:
        drivers.register(FakeHarnessDriver())
    if include_codex_driver:
        codex_environment = {"CODEX_HOME": str(effective_config.codex_home)}

        def client_factory(_execution: ExecutionContract) -> OpenAICodexClient:
            return OpenAICodexClient(environment=codex_environment)

        supervisor = RunSupervisor(
            store,
            client_factory=client_factory,
            model=effective_config.model,
            effort=effective_config.reasoning_effort,
            toolchain_read_roots=(
                "/opt/agentd/venv",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/lib",
                "/lib",
            ),
        )
        drivers.register(CodexSdkDriver(supervisor))
        account_oracle = CodexAccountOracle(
            "codex",
            client_factory=lambda: OpenAICodexClient(environment=codex_environment),
            store=store,
        )
    if include_codex_cli_driver:
        drivers.register(CodexCliDriver())
    provisioner = (
        TrustedUvProvisioner(
            codex_home=effective_config.codex_home,
            state_directory=effective_config.database.parent,
            cache_directory=effective_config.uv_cache,
            provisioning_home=effective_config.workspace_root / ".provision-home",
        )
        if trusted_provisioning
        else None
    )
    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(workspace_root),
        drivers,
        backends=BackendRegistry((LocalWorkerBackend(),)),
        provisioner=provisioner,
        enforce_codex_account_policy=enforce_codex_account_policy,
        account_policy=AccountPolicyThresholds(
            snapshot_stale_after=timedelta(
                seconds=effective_config.account_stale_seconds
            )
        ),
        usage_policy=JobUsagePolicy(top_up_chunk=effective_config.quota_top_up_tokens),
        hard_cap_grace=timedelta(seconds=effective_config.hard_cap_grace_seconds),
    )
    control_plane = ControlPlane(store, coordinator=coordinator)
    return LocalRuntime(
        store,
        coordinator,
        control_plane,
        drivers,
        supervisor,
        account_oracle,
    )
