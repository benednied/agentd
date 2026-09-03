"""Remote worker backend backed by the persistent typed worker protocol."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from agentd.domain.enums import NodeState
from agentd.domain.models import (
    ExecutionContract,
    RunHandle,
    RunObservation,
    RunResult,
    WorkerNode,
)
from agentd.harness.protocol import HarnessDriver
from agentd.workers.client import RemoteWorkerClient
from agentd.workers.errors import WorkerOperationError, WorkerProtocolError
from agentd.workers.local import _normalized_architecture, _normalized_os
from agentd.workers.protocol import WorkerBackendCapabilities
from agentd.workers.remote_protocol import MAX_STRING_SIZE

_CAPABILITY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


class RemoteWorkerBackend:
    """Address a worker daemon without exposing transport details to callers."""

    def __init__(
        self,
        client: RemoteWorkerClient,
        *,
        name: str = "remote",
        node_id: str | None = None,
        operating_system: str | None = None,
        architecture: str | None = None,
        features: frozenset[str] = frozenset(),
        expected_driver: str = "operations",
    ) -> None:
        if not isinstance(expected_driver, str) or not expected_driver.strip():
            raise ValueError("expected_driver must be a non-empty name")
        self._client = client
        self._node_id = node_id or client.node_id
        self._expected_driver = expected_driver
        # Endpoint configuration may constrain what the operator expects, but
        # it is never an authority for capabilities. Only the authenticated
        # worker heartbeat can populate this set.
        self._required_features = frozenset(features)
        self._operating_system = (
            _normalized_os(operating_system) if operating_system is not None else None
        )
        self._architecture = (
            _normalized_architecture(architecture) if architecture is not None else None
        )
        self._capabilities = WorkerBackendCapabilities(
            name=name,
            supported_operating_systems=(
                frozenset({self._operating_system})
                if self._operating_system is not None
                else frozenset()
            ),
            supported_architectures=(
                frozenset({self._architecture})
                if self._architecture is not None
                else frozenset()
            ),
            # The transport itself is known locally; worker operation
            # capabilities remain absent until an authenticated heartbeat.
            features=frozenset({"remote-protocol"}),
            remote=True,
        )
        self._run_ids: dict[str, str] = {}
        self._healthy = False

    @property
    def client(self) -> RemoteWorkerClient:
        """Expose the connection for registration/heartbeat orchestration."""

        return self._client

    def capabilities(self) -> WorkerBackendCapabilities:
        return self._capabilities

    def is_compatible(self, node: WorkerNode) -> bool:
        if not self._healthy:
            return False
        if node.state is not NodeState.ONLINE:
            return False
        if self._node_id is not None and node.id != self._node_id:
            return False
        backend_label = node.labels.get("backend")
        if backend_label != self._capabilities.name:
            return False
        operating_system = node.labels.get("os")
        if (
            self._capabilities.supported_operating_systems
            and operating_system is not None
            and (
                _normalized_os(operating_system)
                not in self._capabilities.supported_operating_systems
            )
        ):
            return False
        architecture = node.labels.get("arch")
        return (
            not self._capabilities.supported_architectures
            or architecture is None
            or (
                _normalized_architecture(architecture)
                in self._capabilities.supported_architectures
            )
        )

    async def dispatch(
        self,
        driver: HarnessDriver,
        contract: ExecutionContract,
        *,
        run_id: str | None = None,
        managed: bool = False,
    ) -> RunHandle:
        if not self._healthy:
            raise WorkerOperationError("remote worker has not passed a heartbeat")
        effective_run_id = run_id or contract.job_id
        handle = await self._client.start(
            driver,
            contract,
            run_id=effective_run_id,
            managed=managed,
        )
        self._run_ids[handle.id] = effective_run_id
        return handle

    async def status(self, run: RunHandle | str) -> dict[str, object]:
        try:
            return await self._client.status(self._remote_run_id(run))
        except Exception:
            # A malformed status or transport failure invalidates the health
            # assertion used by placement.  The next heartbeat must re-bind
            # this backend before new work can be dispatched.
            self._healthy = False
            raise

    async def observe(self, run: RunHandle | str) -> RunObservation | None:
        return await self._client.observe(self._remote_run_id(run))

    async def steer(self, run: RunHandle | str, instruction: str) -> None:
        await self._client.steer(
            self._remote_run_id(run),
            instruction,
        )

    async def interrupt(self, run: RunHandle | str) -> None:
        await self._client.interrupt(self._remote_run_id(run))

    async def cancel(self, run: RunHandle | str) -> None:
        await self._client.cancel(self._remote_run_id(run))

    async def collect(self, run: RunHandle | str) -> RunResult:
        return await self._client.collect(self._remote_run_id(run))

    async def heartbeat(self) -> dict[str, Any]:
        try:
            snapshot = await self._client.heartbeat()
            features = self._validate_heartbeat(snapshot)
            self._capabilities = replace(
                self._capabilities,
                features=frozenset({"remote-protocol", *features}),
            )
        except Exception:
            self._healthy = False
            raise
        self._healthy = True
        return snapshot

    async def close(self) -> None:
        self._healthy = False
        await self._client.close()

    def _validate_heartbeat(self, snapshot: object) -> frozenset[str]:
        if not isinstance(snapshot, Mapping):
            raise WorkerProtocolError("worker heartbeat is malformed")
        required_fields = {
            "node_id",
            "session_epoch",
            "drivers",
            "driver_features",
            "active_runs",
        }
        if set(snapshot) != required_fields:
            raise WorkerProtocolError("worker heartbeat fields are invalid")
        node_id = snapshot.get("node_id")
        session_epoch = snapshot.get("session_epoch")
        expected_session = getattr(self._client, "session_epoch", None)
        drivers = snapshot.get("drivers")
        driver_features = snapshot.get("driver_features")
        active_runs = snapshot.get("active_runs")
        if (
            node_id != self._node_id
            or not isinstance(session_epoch, str)
            or not session_epoch.strip()
            or session_epoch != expected_session
            or not isinstance(drivers, list)
            or any(not isinstance(driver, str) for driver in drivers)
            or len(set(drivers)) != len(drivers)
            or any(
                not driver.strip()
                or len(driver) > MAX_STRING_SIZE
                or _CAPABILITY_NAME_RE.fullmatch(driver) is None
                for driver in drivers
            )
            or self._expected_driver not in drivers
            or not isinstance(driver_features, Mapping)
            or set(driver_features) != set(drivers)
            or isinstance(active_runs, bool)
            or not isinstance(active_runs, int)
            or active_runs < 0
        ):
            raise WorkerProtocolError(
                "worker heartbeat identity or capabilities invalid"
            )

        normalized: dict[str, frozenset[str]] = {}
        for driver in drivers:
            raw_features = driver_features.get(driver)
            if (
                not isinstance(raw_features, list)
                or any(
                    not isinstance(feature, str)
                    or not feature.strip()
                    or len(feature) > MAX_STRING_SIZE
                    or _CAPABILITY_NAME_RE.fullmatch(feature) is None
                    for feature in raw_features
                )
                or len(set(raw_features)) != len(raw_features)
            ):
                raise WorkerProtocolError(
                    "worker heartbeat driver features are invalid"
                )
            normalized[driver] = frozenset(raw_features)

        features = normalized[self._expected_driver]
        if not self._required_features <= features:
            missing = sorted(self._required_features - features)
            raise WorkerProtocolError(
                "worker heartbeat is missing configured capabilities: "
                + ", ".join(missing)
            )
        return features

    def _remote_run_id(self, run: RunHandle | str) -> str:
        if isinstance(run, str):
            return run
        return self._run_ids.get(run.id, run.id)


__all__ = ["RemoteWorkerBackend"]
