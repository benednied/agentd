from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.domain.enums import NodeState
from agentd.domain.models import ExecutionContract, ResourceVector, WorkerNode
from agentd.harness.fake import FakeHarnessDriver
from agentd.workers import (
    BackendAlreadyRegisteredError,
    BackendRegistry,
    LocalWorkerBackend,
    UnknownBackendError,
    WorkerBackend,
    WorkerBackendCapabilities,
)


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


def test_compatibility_does_not_duplicate_resource_or_harness_policy() -> None:
    backend = LocalWorkerBackend(operating_system="linux", architecture="x86_64")
    node = _node()

    assert node.harnesses == frozenset()
    assert backend.is_compatible(node)


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
