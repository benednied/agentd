from __future__ import annotations

import asyncio
import os
import ssl
from pathlib import Path

import pytest

from agentd.domain.models import ResourceVector, WorkerNode
from agentd.harness.protocol import HarnessDriver
from agentd.workers.controller import (
    OperationsHarnessDescriptor,
    RemoteWorkerController,
    RemoteWorkerEndpoint,
)
from agentd.workers.errors import WorkerOperationError, WorkerProtocolError
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE
from agentd.workers.remote import RemoteWorkerBackend


def _document(*, name: str = "worker-a", **changes: object) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "name": name,
        "host": "127.0.0.1",
        "port": 8765,
        "node_id": "node-a",
        "session_epoch": "epoch-a",
        "psk_env": "REMOTE_PSK",
        "allow_insecure_loopback": True,
    }
    endpoint.update(changes)
    return {"workers": [endpoint]}


def test_controller_composes_remote_backend_and_capability_only_driver(
    tmp_path: Path,
) -> None:
    empty = RemoteWorkerController.from_document(
        {"workers": []},
        values={},
        base_dir=tmp_path,
    )
    assert empty.backends == ()
    assert empty.drivers == ()

    controller = RemoteWorkerController.from_document(
        _document(features=["custom-feature"]),
        values={"REMOTE_PSK": "r" * 32},
        base_dir=tmp_path,
    )
    assert len(controller.backends) == 1
    backend = controller.backends[0]
    assert backend.capabilities().name == "worker-a"
    assert backend.capabilities().remote is True
    # Endpoint config is not a capability authority. Until the authenticated
    # worker heartbeat arrives, only the transport feature is known.
    assert backend.capabilities().features == frozenset({"remote-protocol"})
    assert backend.capabilities().supported_operating_systems == frozenset()
    assert backend.capabilities().supported_architectures == frozenset()

    node = WorkerNode(
        id="node-a",
        labels={"backend": "worker-a", "os": "linux", "arch": "x86_64"},
        capacity=ResourceVector(cpu=1, ram_gb=1),
        harnesses=frozenset({"operations"}),
    )
    assert not backend.is_compatible(node)
    descriptor = controller.operations_descriptor
    assert isinstance(descriptor, OperationsHarnessDescriptor)
    assert isinstance(descriptor, HarnessDriver)
    assert descriptor.capabilities().features == frozenset(
        {ARTIFACT_VERIFICATION_FEATURE, "build-image", "deploy-image"}
    )
    with pytest.raises(WorkerOperationError, match="capability-only"):
        asyncio.run(descriptor.start(None))  # type: ignore[arg-type]

    asyncio.run(controller.close())
    assert all(client._closed for client in controller.clients)


def test_empty_controller_does_not_enable_operations_driver() -> None:
    controller = RemoteWorkerController.from_environment({})

    assert controller.backends == ()
    assert controller.drivers == ()


def test_controller_rejects_reserved_local_backend_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        RemoteWorkerController.from_document(
            _document(name="local"),
            values={"REMOTE_PSK": "r" * 32},
            base_dir=tmp_path,
        )


def test_remote_backend_health_requires_authenticated_operations_heartbeat() -> None:
    class FakeClient:
        node_id = "node-a"
        session_epoch = "epoch-a"

        def __init__(self) -> None:
            self.snapshot: object = {
                "node_id": "node-a",
                "session_epoch": "epoch-a",
                "drivers": ["operations"],
                "driver_features": {"operations": []},
                "active_runs": 0,
            }

        async def heartbeat(self) -> object:
            return self.snapshot

        async def close(self) -> None:
            return None

    client = FakeClient()
    backend = RemoteWorkerBackend(
        client,  # type: ignore[arg-type]
        name="worker-a",
        node_id="node-a",
    )
    node = WorkerNode(
        id="node-a",
        labels={"backend": "worker-a"},
        capacity=ResourceVector(cpu=1, ram_gb=1),
        harnesses=frozenset(),
    )
    assert not backend.is_compatible(node)
    client.snapshot = {
        "node_id": "node-a",
        "session_epoch": "epoch-a",
        "drivers": ["other"],
        "driver_features": {"other": []},
        "active_runs": 0,
    }
    with pytest.raises(WorkerProtocolError, match="capabilities invalid"):
        asyncio.run(backend.heartbeat())
    assert not backend.is_compatible(node)
    client.snapshot = {
        "node_id": "node-a",
        "session_epoch": "epoch-a",
        "drivers": ["operations"],
        "driver_features": {"operations": ["build-image"]},
        "active_runs": 0,
    }
    asyncio.run(backend.heartbeat())
    assert backend.is_compatible(node)
    assert backend.capabilities().features == frozenset(
        {"remote-protocol", "build-image"}
    )
    assert ARTIFACT_VERIFICATION_FEATURE not in backend.capabilities().features

    client.snapshot = {
        "node_id": "node-a",
        "session_epoch": "epoch-a",
        "drivers": ["operations"],
        "driver_features": {"operations": ["build-image", "build-image"]},
        "active_runs": 0,
    }
    with pytest.raises(WorkerProtocolError, match="driver features"):
        asyncio.run(backend.heartbeat())
    assert not backend.is_compatible(node)

    client.snapshot = {
        "node_id": "another-node",
        "session_epoch": "epoch-a",
        "drivers": ["operations"],
        "driver_features": {"operations": []},
        "active_runs": 0,
    }
    with pytest.raises(WorkerProtocolError, match="identity"):
        asyncio.run(backend.heartbeat())
    assert not backend.is_compatible(node)


def test_controller_rejects_inline_secrets_duplicates_and_duplicate_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="inline"):
        RemoteWorkerController.from_document(
            {"workers": [{**_document()["workers"][0], "psk": "r" * 32}]},
            values={"REMOTE_PSK": "r" * 32},
            base_dir=tmp_path,
        )

    duplicate = tmp_path / "remote.json"
    duplicate.write_text(
        '{"workers": [], "workers": []}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid JSON"):
        RemoteWorkerController.from_environment(
            {"AGENTD_REMOTE_WORKERS_CONFIG": str(duplicate)}
        )

    first = _document()["workers"][0]
    second = dict(first)
    second["node_id"] = "node-b"
    with pytest.raises(ValueError, match="names must be unique"):
        RemoteWorkerController.from_document(
            {"workers": [first, second]},
            values={"REMOTE_PSK": "r" * 32},
            base_dir=tmp_path,
        )

    second["name"] = "worker-b"
    second["psk_env"] = "OTHER_REMOTE_PSK"
    with pytest.raises(ValueError, match="PSK values must be unique"):
        RemoteWorkerController.from_document(
            {"workers": [first, second]},
            values={
                "REMOTE_PSK": "r" * 32,
                "OTHER_REMOTE_PSK": "r" * 32,
            },
            base_dir=tmp_path,
        )

    too_many = []
    for index in range(129):
        item = dict(first)
        item["name"] = f"worker-{index}"
        item["node_id"] = f"node-{index}"
        too_many.append(item)
    with pytest.raises(ValueError, match="count exceeds"):
        RemoteWorkerController.from_document(
            {"workers": too_many},
            values={"REMOTE_PSK": "r" * 32},
            base_dir=tmp_path,
        )


def test_endpoint_secret_file_and_tls_defaults(tmp_path: Path) -> None:
    secret_file = tmp_path / "worker.psk"
    secret_file.write_bytes(b"f" * 32)
    os.chmod(secret_file, 0o600)
    endpoint = RemoteWorkerEndpoint(
        name="worker-a",
        host="worker.example",
        port=8765,
        node_id="node-a",
        session_epoch="epoch-a",
        psk_file=secret_file,
    )
    assert endpoint.load_secret({}) == b"f" * 32
    os.chmod(secret_file, 0o400)
    with pytest.raises(ValueError, match="0600"):
        endpoint.load_secret({})
    symlink = tmp_path / "worker-link.psk"
    symlink.symlink_to(secret_file)
    with pytest.raises(ValueError, match="symlink"):
        RemoteWorkerEndpoint(
            name="worker-c",
            host="worker.example",
            port=8765,
            node_id="node-c",
            session_epoch="epoch-c",
            psk_file=symlink,
        ).load_secret({})

    context = RemoteWorkerEndpoint(
        name="worker-b",
        host="worker.example",
        port=8765,
        node_id="node-b",
        session_epoch="epoch-b",
        psk_env="REMOTE_PSK",
    ).tls_context()
    assert context is not None
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2

    with pytest.raises(ValueError, match="restricted to loopback"):
        RemoteWorkerController.from_document(
            _document(host="worker.example"),
            values={"REMOTE_PSK": "r" * 32},
            base_dir=tmp_path,
        )
