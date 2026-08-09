"""Composition helpers for a local agentd process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentd.coordinator import SchedulerCoordinator
from agentd.harness.codex import CodexDriver
from agentd.harness.fake import FakeHarnessDriver
from agentd.harness.registry import DriverRegistry
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

    def close(self) -> None:
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
) -> LocalRuntime:
    """Compose the local SQLite/Git/process implementation behind domain ports."""

    database_path = Path(database)
    if str(database) != ":memory:":
        database_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStateStore(database)
    drivers = DriverRegistry()
    if include_fake_driver:
        drivers.register(FakeHarnessDriver())
    if include_codex_driver:
        drivers.register(CodexDriver())
    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(workspace_root),
        drivers,
        backends=BackendRegistry((LocalWorkerBackend(),)),
    )
    control_plane = ControlPlane(store, coordinator=coordinator)
    return LocalRuntime(store, coordinator, control_plane, drivers)
