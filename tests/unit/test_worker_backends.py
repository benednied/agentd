from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.domain.enums import NodeState, RunOutcome
from agentd.domain.models import (
    ExecutionContract,
    ResourceVector,
    RunHandle,
    RunResult,
    WorkerNode,
)
from agentd.harness.fake import FakeHarnessDriver
from agentd.workers import (
    BackendAlreadyRegisteredError,
    BackendRegistry,
    LocalWorkerBackend,
    RemoteWorkerBackend,
    RemoteWorkerClient,
    UnknownBackendError,
    WorkerBackend,
    WorkerBackendCapabilities,
)


class _StatusFakeHarnessDriver(FakeHarnessDriver):
    def status(self, run: RunHandle) -> dict[str, object]:
        self.execution_for(run)
        return {"known": True, "terminal": False, "result": None}


def _node(
    *,
    node_id: str = "local-1",
    operating_system: str = "linux",
    architecture: str = "x86_64",
    backend: str = "local",
    state: NodeState = NodeState.ONLINE,
) -> WorkerNode:
    return WorkerNode(
        id=node_id,
        labels={
            "os": operating_system,
            "arch": architecture,
            "backend": backend,
        },
        capacity=ResourceVector(cpu=8, ram_gb=16),
        harnesses=frozenset(),
        state=state,
    )


def _contract(working_directory: Path) -> ExecutionContract:
    return ExecutionContract(
        job_id="job-1",
        objective="Exercise local dispatch",
        scope="tests only",
        acceptance_criteria=("driver receives the contract",),
        dependency_results={},
        role="implementer",
        allowed_filesystem_scope=(str(working_directory),),
        checkpoint_expectations="checkpoint on request",
        coordination_mechanisms=("control-plane",),
        completion_protocol="return a run result",
        working_directory=str(working_directory),
        environment={},
        # Deliberately unsupported by FakeHarnessDriver. The backend must not
        # duplicate scheduler/harness model-selection policy.
        model_class="premium",
    )


def test_local_backend_exposes_metadata_and_matches_addressable_nodes() -> None:
    backend = LocalWorkerBackend(
        node_id="local-1",
        operating_system="Linux",
        architecture="AMD64",
    )

    assert isinstance(backend, WorkerBackend)
    assert backend.capabilities() == WorkerBackendCapabilities(
        name="local",
        supported_operating_systems=frozenset({"linux"}),
        supported_architectures=frozenset({"x86_64"}),
        features=frozenset({"local-process"}),
    )
    assert backend.is_compatible(_node())
    assert not backend.is_compatible(_node(node_id="different"))
    assert not backend.is_compatible(_node(operating_system="windows"))
    assert not backend.is_compatible(_node(architecture="arm64"))
    assert not backend.is_compatible(_node(backend="ssh"))
    assert not backend.is_compatible(_node(state=NodeState.OFFLINE))


def test_local_dispatch_is_a_thin_harness_start(tmp_path: Path) -> None:
    backend = LocalWorkerBackend(operating_system="linux", architecture="x86_64")
    driver = FakeHarnessDriver(id_factory=lambda: "run-1")
    contract = _contract(tmp_path)

    handle = asyncio.run(backend.dispatch(driver, contract))

    assert handle.id == "run-1"
    assert driver.execution_for(handle) == contract
    assert tuple(call.operation for call in driver.calls_for(handle)) == ("start",)


def test_local_backend_lifecycle_helpers_remain_driver_typed(tmp_path: Path) -> None:
    backend = LocalWorkerBackend(operating_system="linux", architecture="x86_64")
    driver = _StatusFakeHarnessDriver(id_factory=lambda: "run-1")
    contract = _contract(tmp_path)

    async def lifecycle() -> tuple[RunResult, tuple[str, ...]]:
        handle = await backend.dispatch(driver, contract, run_id="durable-1")
        assert await backend.observe("durable-1") is None
        assert await backend.status("durable-1") == {
            "known": True,
            "terminal": False,
            "result": None,
        }
        await backend.steer("durable-1", "keep going")
        await backend.interrupt(handle)
        await backend.cancel("durable-1")
        result = await backend.collect(handle)
        return result, tuple(call.operation for call in driver.calls_for(handle))

    result, calls = asyncio.run(lifecycle())

    assert result.outcome.value == "cancelled"
    assert calls == (
        "start",
        "steer",
        "interrupt",
        "cancel",
        "collect",
    )
    assert asyncio.run(backend.heartbeat()) == {
        "node_id": None,
        "backend": "local",
        "remote": False,
    }
    assert asyncio.run(backend.close()) is None


def test_compatibility_does_not_duplicate_resource_or_harness_policy() -> None:
    backend = LocalWorkerBackend(operating_system="linux", architecture="x86_64")
    node = _node()

    assert node.harnesses == frozenset()
    assert backend.is_compatible(node)


def test_remote_backend_is_explicitly_remote_and_capability_addressable() -> None:
    client = RemoteWorkerClient(
        "127.0.0.1",
        45_678,
        node_id="remote-1",
        session_epoch="epoch-1",
        secret=b"r" * 32,
        allow_insecure_loopback=True,
    )
    backend = RemoteWorkerBackend(
        client,
        operating_system="linux",
        architecture="x86_64",
    )

    assert isinstance(backend, WorkerBackend)
    assert backend.capabilities().remote is True
    assert "remote-protocol" in backend.capabilities().features
    assert backend.capabilities().supported_operating_systems == frozenset({"linux"})
    assert backend.capabilities().supported_architectures == frozenset({"x86_64"})
    assert not backend.is_compatible(_node(node_id="remote-1", backend="remote"))
    assert not backend.is_compatible(_node(node_id="remote-2", backend="remote"))
    unknown_platform = RemoteWorkerBackend(client)
    assert unknown_platform.capabilities().supported_operating_systems == frozenset()
    assert unknown_platform.capabilities().supported_architectures == frozenset()


def test_remote_backend_heartbeat_and_close_delegate_to_client() -> None:
    class FakeClient:
        node_id = "remote-1"
        session_epoch = "epoch-1"

        def __init__(self) -> None:
            self.closed = False

        async def heartbeat(self) -> dict[str, object]:
            return {
                "node_id": self.node_id,
                "session_epoch": "epoch-1",
                "drivers": ["operations"],
                "driver_features": {"operations": []},
                "active_runs": 0,
            }

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()
    backend = RemoteWorkerBackend(client)  # type: ignore[arg-type]

    assert asyncio.run(backend.heartbeat()) == {
        "node_id": "remote-1",
        "session_epoch": "epoch-1",
        "drivers": ["operations"],
        "driver_features": {"operations": []},
        "active_runs": 0,
    }
    assert asyncio.run(backend.close()) is None
    assert client.closed is True
    assert not backend.is_compatible(_node(node_id="remote-1", backend="remote"))


def test_remote_backend_routes_durable_ids_without_handle_mapping(
    tmp_path: Path,
) -> None:
    class FakeClient:
        node_id = "remote-1"
        session_epoch = "epoch-1"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def start(self, _driver, _contract, *, run_id, managed=False):
            del managed
            self.calls.append(("start", run_id))
            return RunHandle(id="worker-handle", driver="fake")

        async def steer(self, run, _instruction):
            self.calls.append(("steer", run))

        async def interrupt(self, run):
            self.calls.append(("interrupt", run))

        async def cancel(self, run):
            self.calls.append(("cancel", run))

        async def collect(self, run):
            self.calls.append(("collect", run))
            return RunResult(RunOutcome.COMPLETED, "done")

    client = FakeClient()
    backend = RemoteWorkerBackend(client)  # type: ignore[arg-type]
    backend._healthy = True
    handle = asyncio.run(
        backend.dispatch(
            FakeHarnessDriver(),
            _contract(tmp_path),
            run_id="durable-run",
        )
    )
    assert handle.id == "worker-handle"

    # A controller restart loses the in-memory handle map. Passing the
    # persisted control-plane run id must still address every remote action.
    backend._run_ids.clear()
    asyncio.run(backend.steer("durable-run", "continue"))
    asyncio.run(backend.interrupt("durable-run"))
    asyncio.run(backend.cancel("durable-run"))
    assert asyncio.run(backend.collect("durable-run")).outcome is RunOutcome.COMPLETED
    assert client.calls == [
        ("start", "durable-run"),
        ("steer", "durable-run"),
        ("interrupt", "durable-run"),
        ("cancel", "durable-run"),
        ("collect", "durable-run"),
    ]


def test_registry_is_keyed_by_capability_name() -> None:
    local = LocalWorkerBackend(operating_system="linux", architecture="x86_64")
    replacement = LocalWorkerBackend(
        operating_system="linux",
        architecture="x86_64",
    )
    registry = BackendRegistry([local])

    assert registry.names() == ("local",)
    assert registry.get("local") is local
    assert registry.capabilities() == (local.capabilities(),)
    assert registry.compatible(_node()) == (local,)
    assert "local" in registry
    assert len(registry) == 1

    with pytest.raises(BackendAlreadyRegisteredError):
        registry.register(replacement)

    registry.register(replacement, replace=True)
    assert registry.get("local") is replacement
    assert registry.remove("local") is replacement

    with pytest.raises(UnknownBackendError):
        registry.get("local")


def test_capability_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WorkerBackendCapabilities(name="   ")
