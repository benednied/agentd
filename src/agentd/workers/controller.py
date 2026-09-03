"""Strict composition of configured remote workers.

The controller owns endpoint configuration and transport lifetimes.  It never
accepts a PSK value in JSON or a command-line argument: each endpoint names an
environment variable or a mode-0600 file from which the secret is loaded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunResult,
)
from agentd.harness.protocol import HarnessDriver
from agentd.workers.client import RemoteWorkerClient
from agentd.workers.errors import WorkerOperationError
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE
from agentd.workers.registry import BackendRegistry
from agentd.workers.remote import RemoteWorkerBackend
from agentd.workers.remote_protocol import MAX_STRING_SIZE

MAX_REMOTE_WORKERS_CONFIG_BYTES = 1_048_576
MAX_REMOTE_WORKERS = 128
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STABLE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_FORBIDDEN_SECRET_KEYS = frozenset(
    {"password", "psk", "secret", "token", "private_key"}
)


def _text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_STRING_SIZE
        or value != value.strip()
        or "\x00" in value
        or any(char.isspace() for char in value)
    ):
        raise ValueError(f"{name} must be a non-empty string without whitespace")
    return value


def _stable_name(value: object, name: str) -> str:
    value = _text(value, name)
    if _STABLE_NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable lowercase name")
    return value


def _path(value: object, name: str, *, base_dir: Path) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    if "\x00" in value:
        raise ValueError(f"{name} contains a NUL")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _secret_path(value: object, *, base_dir: Path) -> Path:
    """Normalize a PSK path without resolving a final symlink."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("psk_file must be a non-empty path")
    if "\x00" in value:
        raise ValueError("psk_file contains a NUL")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return Path(os.path.abspath(candidate))


def _optional_path(
    value: object,
    name: str,
    *,
    base_dir: Path,
) -> Path | None:
    if value is None:
        return None
    return _path(value, name, base_dir=base_dir)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("remote worker config contains duplicate keys")
        result[key] = value
    return result


def _contains_inline_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                normalized = key.casefold()
                if normalized in _FORBIDDEN_SECRET_KEYS or any(
                    marker in normalized for marker in ("password", "secret", "token")
                ):
                    return True
            if _contains_inline_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_inline_secret(item) for item in value)
    return False


def _parse_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _parse_features(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("features must be a JSON list of names")
    if len(set(value)) != len(value):
        raise ValueError("features must not contain duplicates")
    return frozenset(_stable_name(item, "feature") for item in value)


@dataclass(frozen=True, slots=True)
class RemoteWorkerEndpoint:
    """One validated, non-secret remote worker endpoint definition."""

    name: str
    host: str
    port: int
    node_id: str
    session_epoch: str
    psk_env: str | None = None
    psk_file: Path | None = None
    tls_ca: Path | None = None
    tls_client_cert: Path | None = None
    tls_client_key: Path | None = None
    server_hostname: str | None = None
    allow_insecure_loopback: bool = False
    operating_system: str | None = None
    architecture: str | None = None
    features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _stable_name(self.name, "worker name")
        if self.name == "local":
            raise ValueError("remote worker name 'local' is reserved")
        _text(self.host, "worker host")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65_535
        ):
            raise ValueError("worker port must be between 1 and 65535")
        _text(self.node_id, "node_id")
        _text(self.session_epoch, "session_epoch")
        if (self.psk_env is None) == (self.psk_file is None):
            raise ValueError("PSK must come from exactly one env name or file")
        if self.psk_file is not None and not isinstance(self.psk_file, Path):
            raise TypeError("psk_file must be a Path")
        for path_name, path_value in (
            ("tls_ca", self.tls_ca),
            ("tls_client_cert", self.tls_client_cert),
            ("tls_client_key", self.tls_client_key),
        ):
            if path_value is not None and not isinstance(path_value, Path):
                raise TypeError(f"{path_name} must be a Path")
        if self.psk_env is not None and _ENV_NAME_RE.fullmatch(self.psk_env) is None:
            raise ValueError("psk_env must be a valid environment variable name")
        if (self.tls_client_cert is None) != (self.tls_client_key is None):
            raise ValueError("TLS client certificate and key must be paired")
        if self.allow_insecure_loopback and self.host not in _LOOPBACK_HOSTS:
            raise ValueError("insecure worker transport is restricted to loopback")
        if self.allow_insecure_loopback and (
            self.tls_ca is not None
            or self.tls_client_cert is not None
            or self.tls_client_key is not None
        ):
            raise ValueError("plaintext endpoint must not configure TLS files")
        for name, value in (
            ("operating_system", self.operating_system),
            ("architecture", self.architecture),
            ("server_hostname", self.server_hostname),
        ):
            if value is not None:
                _text(value, name)
        if any(
            not isinstance(feature, str) or _STABLE_NAME_RE.fullmatch(feature) is None
            for feature in self.features
        ):
            raise ValueError("features must contain stable lowercase names")

    def load_secret(self, values: Mapping[str, str]) -> bytes:
        if self.psk_file is not None:
            try:
                if self.psk_file.is_symlink():
                    raise ValueError("PSK file must not be a symlink")
                if not self.psk_file.is_file():
                    raise ValueError("PSK file must be a regular file")
                if self.psk_file.stat().st_mode & 0o777 != 0o600:
                    raise ValueError("PSK file must be mode 0600")
                secret = self.psk_file.read_bytes()
            except OSError as error:
                raise ValueError("PSK file cannot be read") from error
        else:
            assert self.psk_env is not None
            raw = values.get(self.psk_env)
            if raw is None:
                raise ValueError("configured PSK environment variable is not set")
            secret = raw.encode("utf-8")
        if len(secret) < 32:
            raise ValueError("PSK must contain at least 32 bytes")
        return secret

    def tls_context(self) -> ssl.SSLContext | None:
        if self.allow_insecure_loopback:
            return None
        if self.tls_ca is not None and not self.tls_ca.is_file():
            raise ValueError("configured TLS CA must be a file")
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=str(self.tls_ca) if self.tls_ca is not None else None,
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        if self.tls_client_cert is not None and self.tls_client_key is not None:
            if not self.tls_client_cert.is_file() or not self.tls_client_key.is_file():
                raise ValueError(
                    "configured TLS client certificate and key must be files"
                )
            try:
                context.load_cert_chain(self.tls_client_cert, self.tls_client_key)
            except (OSError, ssl.SSLError) as error:
                raise ValueError(
                    "configured TLS client certificate cannot be loaded"
                ) from error
        return context

    def create_client(self, values: Mapping[str, str]) -> RemoteWorkerClient:
        return self._create_client(self.load_secret(values))

    def _create_client(self, secret: bytes) -> RemoteWorkerClient:
        return RemoteWorkerClient(
            self.host,
            self.port,
            node_id=self.node_id,
            session_epoch=self.session_epoch,
            secret=secret,
            ssl_context=self.tls_context(),
            server_hostname=self.server_hostname or self.host,
            allow_insecure_loopback=self.allow_insecure_loopback,
        )


class OperationsHarnessDescriptor:
    """Capability-only descriptor; it must never execute on the controller."""

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name="operations",
            models=frozenset({"standard"}),
            features=frozenset(
                {ARTIFACT_VERIFICATION_FEATURE, "build-image", "deploy-image"}
            ),
            native_pause=False,
            steering=False,
            checkpointing=False,
        )

    def _fail(self) -> Never:
        raise WorkerOperationError(
            "the operations descriptor is capability-only and cannot run locally"
        )

    async def start(self, execution: ExecutionContract) -> RunHandle:
        del execution
        self._fail()

    async def steer(self, run: RunHandle, instruction: str) -> None:
        del run, instruction
        self._fail()

    async def interrupt(self, run: RunHandle) -> None:
        del run
        self._fail()

    async def collect(self, run: RunHandle) -> RunResult:
        del run
        self._fail()

    async def cancel(self, run: RunHandle) -> None:
        del run
        self._fail()


class RemoteWorkerController:
    """Own validated remote endpoints, clients, backends, and descriptors."""

    def __init__(
        self,
        endpoints: Iterable[RemoteWorkerEndpoint],
        *,
        values: Mapping[str, str] | None = None,
    ) -> None:
        endpoint_values = tuple(endpoints)
        if len(endpoint_values) > MAX_REMOTE_WORKERS:
            raise ValueError(f"remote worker count exceeds {MAX_REMOTE_WORKERS}")
        names = [endpoint.name for endpoint in endpoint_values]
        node_ids = [endpoint.node_id for endpoint in endpoint_values]
        if len(set(names)) != len(names):
            raise ValueError("remote worker names must be unique")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("remote worker node_id values must be unique")
        environment = os.environ if values is None else values
        # Client construction opens no network resources, so composition can
        # remain synchronous even when one endpoint's secret or TLS files are
        # invalid.
        secrets = tuple(
            endpoint.load_secret(environment) for endpoint in endpoint_values
        )
        if len(set(secrets)) != len(secrets):
            raise ValueError("remote worker PSK values must be unique per node")
        clients = [
            endpoint._create_client(secret)
            for endpoint, secret in zip(endpoint_values, secrets, strict=True)
        ]
        self._endpoints = endpoint_values
        self._clients = tuple(clients)
        self._backends = tuple(
            RemoteWorkerBackend(
                client,
                name=endpoint.name,
                node_id=endpoint.node_id,
                operating_system=endpoint.operating_system,
                architecture=endpoint.architecture,
                # Endpoint features are requirements/hints only. The remote
                # backend starts with no operation capability and adopts the
                # registered driver's feature set only after authenticated
                # heartbeat validation.
                features=endpoint.features,
                expected_driver="operations",
            )
            for endpoint, client in zip(endpoint_values, clients, strict=True)
        )
        self._operations = OperationsHarnessDescriptor()

    @classmethod
    def from_environment(
        cls,
        values: Mapping[str, str] | None = None,
    ) -> RemoteWorkerController:
        environment = os.environ if values is None else values
        raw_path = environment.get("AGENTD_REMOTE_WORKERS_CONFIG")
        if not raw_path:
            return cls((), values=environment)
        config_path = Path(raw_path).expanduser().resolve()
        try:
            if config_path.stat().st_size > MAX_REMOTE_WORKERS_CONFIG_BYTES:
                raise ValueError("remote worker config exceeds the size limit")
            document = json.loads(
                config_path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except ValueError as error:
            if str(error) == "remote worker config exceeds the size limit":
                raise
            raise ValueError("remote worker config is not valid JSON") from error
        except (OSError, UnicodeError, TypeError) as error:
            raise ValueError("remote worker config cannot be read") from error
        return cls.from_document(
            document,
            values=environment,
            base_dir=config_path.parent,
        )

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        values: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
    ) -> RemoteWorkerController:
        if _contains_inline_secret(document):
            raise ValueError("inline PSK/secret values are not allowed")
        if not isinstance(document, Mapping) or set(document) != {"workers"}:
            raise ValueError("remote worker config must contain only workers")
        raw_workers = document["workers"]
        if not isinstance(raw_workers, list):
            raise ValueError("remote workers must be a JSON list")
        if len(raw_workers) > MAX_REMOTE_WORKERS:
            raise ValueError(f"remote worker count exceeds {MAX_REMOTE_WORKERS}")
        root = Path.cwd() if base_dir is None else base_dir
        endpoints = tuple(
            _endpoint_from_mapping(item, base_dir=root) for item in raw_workers
        )
        return cls(endpoints, values=values)

    @property
    def endpoints(self) -> tuple[RemoteWorkerEndpoint, ...]:
        return self._endpoints

    @property
    def clients(self) -> tuple[RemoteWorkerClient, ...]:
        return self._clients

    @property
    def backends(self) -> tuple[RemoteWorkerBackend, ...]:
        return self._backends

    @property
    def backend_registry(self) -> BackendRegistry:
        return BackendRegistry(self._backends)

    @property
    def operations_descriptor(self) -> OperationsHarnessDescriptor:
        return self._operations

    @property
    def drivers(self) -> tuple[HarnessDriver, ...]:
        return (self._operations,) if self._endpoints else ()

    async def heartbeat(self) -> tuple[dict[str, object], ...]:
        snapshots: list[dict[str, object]] = []
        for backend in self._backends:
            snapshots.append(await backend.heartbeat())
        return tuple(snapshots)

    async def close(self) -> None:
        results = await asyncio.gather(
            *(backend.close() for backend in self._backends),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                raise result


def _endpoint_from_mapping(
    value: object,
    *,
    base_dir: Path,
) -> RemoteWorkerEndpoint:
    if not isinstance(value, Mapping):
        raise ValueError("each remote worker must be a JSON object")
    allowed = {
        "name",
        "host",
        "port",
        "node_id",
        "session_epoch",
        "psk_env",
        "psk_file",
        "tls_ca",
        "tls_client_cert",
        "tls_client_key",
        "server_hostname",
        "allow_insecure_loopback",
        "operating_system",
        "architecture",
        "features",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("remote worker config contains unknown fields")
    required = {"name", "host", "port", "node_id", "session_epoch"}
    if not required <= set(value):
        raise ValueError("remote worker config is missing required fields")
    port = value["port"]
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("worker port must be an integer")
    psk_env = value.get("psk_env")
    if psk_env is not None and not isinstance(psk_env, str):
        raise ValueError("psk_env must be a string")
    return RemoteWorkerEndpoint(
        name=_stable_name(value["name"], "worker name"),
        host=_text(value["host"], "worker host"),
        port=port,
        node_id=_text(value["node_id"], "node_id"),
        session_epoch=_text(value["session_epoch"], "session_epoch"),
        psk_env=psk_env,
        psk_file=(
            _secret_path(value["psk_file"], base_dir=base_dir)
            if value.get("psk_file") is not None
            else None
        ),
        tls_ca=_optional_path(value.get("tls_ca"), "tls_ca", base_dir=base_dir),
        tls_client_cert=_optional_path(
            value.get("tls_client_cert"), "tls_client_cert", base_dir=base_dir
        ),
        tls_client_key=_optional_path(
            value.get("tls_client_key"), "tls_client_key", base_dir=base_dir
        ),
        server_hostname=(
            _text(value["server_hostname"], "server_hostname")
            if value.get("server_hostname") is not None
            else None
        ),
        allow_insecure_loopback=_parse_bool(
            value.get("allow_insecure_loopback", False),
            "allow_insecure_loopback",
        ),
        operating_system=(
            _text(value["operating_system"], "operating_system")
            if value.get("operating_system") is not None
            else None
        ),
        architecture=(
            _text(value["architecture"], "architecture")
            if value.get("architecture") is not None
            else None
        ),
        features=_parse_features(value.get("features")),
    )


__all__ = [
    "MAX_REMOTE_WORKERS",
    "MAX_REMOTE_WORKERS_CONFIG_BYTES",
    "OperationsHarnessDescriptor",
    "RemoteWorkerController",
    "RemoteWorkerEndpoint",
]
