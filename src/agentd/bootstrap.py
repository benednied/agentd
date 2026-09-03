"""Composition helpers for a local agentd process."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
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
from agentd.harness.protocol import HarnessDriver
from agentd.harness.registry import DriverRegistry
from agentd.harness.supervisor import RunSupervisor
from agentd.provisioning import TrustedUvProvisioner
from agentd.runtime.accounts import AccountPolicyThresholds, JobUsagePolicy
from agentd.runtime.codex_oracle import CodexAccountOracle
from agentd.runtime.governor import ProviderStopPolicy
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.local import LocalWorkerBackend
from agentd.workers.protocol import WorkerBackend
from agentd.workers.registry import BackendRegistry
from agentd.workspaces.git import GitWorkspaceManager

_PRODUCTION_MODEL = "gpt-5.6-terra"
_PRODUCTION_REASONING_EFFORT = "medium"


@dataclass(slots=True)
class LocalRuntime:
    """Owned components of one local control-plane process."""

    store: SQLiteStateStore
    coordinator: SchedulerCoordinator
    control_plane: ControlPlane
    drivers: DriverRegistry
    backends: BackendRegistry
    supervisor: RunSupervisor | None = None
    account_oracle: CodexAccountOracle | None = None

    async def aclose(self) -> None:
        """Quiesce driver transports before closing durable state."""

        await self._close_transports()
        self.store.close()

    async def _close_transports(self) -> None:
        if self.supervisor is not None:
            await self.supervisor.close()
        for name in self.backends.names():
            close = getattr(self.backends.get(name), "close", None)
            if close is not None:
                result = close()
                if result is not None:
                    await result

    def close(self) -> None:
        """Close a runtime from synchronous embedding code."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._close_transports())
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
    additional_drivers: Iterable[HarnessDriver] = (),
    worker_backends: Iterable[WorkerBackend] = (),
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
    if trusted_provisioning or enforce_codex_account_policy:
        if effective_config.model != _PRODUCTION_MODEL:
            raise ValueError(
                f"Production deployment model is fixed to {_PRODUCTION_MODEL!r}"
            )
        if effective_config.reasoning_effort != _PRODUCTION_REASONING_EFFORT:
            raise ValueError(
                "Production deployment reasoning effort is fixed to "
                f"{_PRODUCTION_REASONING_EFFORT!r}"
            )
    store = SQLiteStateStore(database)
    drivers = DriverRegistry()
    supervisor: RunSupervisor | None = None
    account_oracle: CodexAccountOracle | None = None
    if include_fake_driver:
        drivers.register(FakeHarnessDriver())
    if include_codex_driver:
        python_install_directory = effective_config.uv_python_install_directory
        codex_environment = {
            "CODEX_HOME": str(effective_config.codex_home),
            "UV_CACHE_DIR": str(effective_config.uv_cache),
            "UV_PYTHON_INSTALL_DIR": str(python_install_directory),
            "UV_PYTHON_PREFERENCE": "only-managed",
        }

        def client_factory(execution: ExecutionContract) -> OpenAICodexClient:
            return OpenAICodexClient(
                environment=codex_environment,
                cwd=execution.working_directory,
            )

        supervisor = RunSupervisor(
            store,
            client_factory=client_factory,
            model=effective_config.model,
            effort=effective_config.reasoning_effort,
        )
        drivers.register(CodexSdkDriver(supervisor, model=effective_config.model))
        account_oracle = CodexAccountOracle(
            "codex",
            client_factory=lambda: OpenAICodexClient(environment=codex_environment),
            store=store,
        )
    if include_codex_cli_driver:
        drivers.register(CodexCliDriver())
    for driver in additional_drivers:
        drivers.register(driver)
    provisioner = (
        TrustedUvProvisioner(
            codex_home=effective_config.codex_home,
            state_directory=effective_config.database.parent,
            cache_directory=effective_config.uv_cache,
            provisioning_home=effective_config.workspace_root / ".provision-home",
            python_install_directory=effective_config.uv_python_install_directory,
            python_version="3.14",
            extras=("dev",),
        )
        if trusted_provisioning
        else None
    )
    backends = BackendRegistry((LocalWorkerBackend(), *tuple(worker_backends)))
    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(workspace_root),
        drivers,
        backends=backends,
        provisioner=provisioner,
        enforce_codex_account_policy=enforce_codex_account_policy,
        account_policy=AccountPolicyThresholds(
            snapshot_stale_after=timedelta(
                seconds=effective_config.account_stale_seconds
            )
        ),
        usage_policy=JobUsagePolicy(top_up_chunk=effective_config.quota_top_up_tokens),
        provider_stop_policy=ProviderStopPolicy(
            remaining_fraction=effective_config.provider_stop_remaining_fraction
        ),
        hard_cap_grace=timedelta(seconds=effective_config.hard_cap_grace_seconds),
    )
    control_plane = ControlPlane(store, coordinator=coordinator)
    return LocalRuntime(
        store,
        coordinator,
        control_plane,
        drivers,
        backends,
        supervisor,
        account_oracle,
    )
