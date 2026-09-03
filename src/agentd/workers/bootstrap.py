"""Production composition for an authenticated worker daemon.

The worker process deliberately has a small configuration surface.  Secrets
are loaded only from an environment variable or a mode-0600 file, while build
and deployment capabilities come from a strict allowlist document (or its
environment equivalent).  There is no SSH/SCP or free-form command input in
this composition layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import ssl
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from agentd.harness.protocol import HarnessDriver
from agentd.workers.operations import (
    DeploymentTarget,
    DockerComposeDeployer,
    DockerImageBuilder,
    OperationHarnessDriver,
)
from agentd.workers.server import WorkerServer

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TARGET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_REGISTRY_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z"
)
_SHELL_CHARS = frozenset(";|&$`<>")
_MAX_CONFIG_BYTES = 1_048_576
_SCP_STYLE_RE = re.compile(r"[^/\s]+@[^:\s]+:")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty untrimmed string")
    if "\x00" in value or any(char.isspace() for char in value):
        raise ValueError(f"{name} contains forbidden whitespace or NUL")
    return value


def _parse_bool(value: str | None, name: str) -> bool:
    if value is None:
        return False
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _safe_repository(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("repository allowlist entries must be non-empty strings")
    if (
        "\x00" in value
        or value.startswith("-")
        or any(char.isspace() for char in value)
    ):
        raise ValueError("repository allowlist entries are not shell-safe")
    if any(char in _SHELL_CHARS for char in value):
        raise ValueError("repository allowlist entries are not shell-safe")
    if value.startswith("ssh://") or _SCP_STYLE_RE.match(value):
        raise ValueError("SSH/SCP repository transports are not allowed")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "file"}:
            raise ValueError("repository URL must use HTTPS or a local file URL")
        if parsed.scheme == "https" and not parsed.hostname:
            raise ValueError("repository HTTPS URL must include a host")
        if parsed.scheme == "file" and not parsed.path:
            raise ValueError("repository file URL must include a path")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("repository URL cannot contain credentials")
    elif re.match(r"^[^/]+@[^/]+:", value):
        raise ValueError("repository cannot use an SSH/scp-style transport")
    return value


def _safe_registry(value: object) -> str:
    if not isinstance(value, str) or _REGISTRY_RE.fullmatch(value) is None:
        raise ValueError("registry allowlist entries must be lowercase repositories")
    return value


def _csv_values(
    raw: str | None,
    name: str,
    validator: Callable[[object], str],
) -> frozenset[str]:
    if raw is None:
        raise ValueError(f"{name} is required")
    values = tuple(item.strip() for item in raw.split(","))
    if not values or any(not item for item in values):
        raise ValueError(f"{name} must contain no empty entries")
    return frozenset(validator(item) for item in values)


def _compose_file_path(value: object) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("compose_file must be a non-empty relative path")
    path = Path(value)
    if (
        "\x00" in value
        or "\\" in value
        or value.startswith("-")
        or any(char.isspace() or char in _SHELL_CHARS for char in value)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("compose_file must be a confined relative path")
    return path


@dataclass(frozen=True, slots=True)
class OperationAllowlist:
    """Strictly validated worker-side operation destinations."""

    repositories: frozenset[str]
    registries: frozenset[str]
    compose_targets: Mapping[str, DeploymentTarget]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> OperationAllowlist:
        del base_dir  # target files are resolved inside immutable Git worktrees
        expected = {"repositories", "registries", "compose_targets"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(
                "operations config must contain exactly repositories, registries, "
                "and compose_targets"
            )
        repositories = value["repositories"]
        registries = value["registries"]
        targets = value["compose_targets"]
        if not isinstance(repositories, list) or not repositories:
            raise ValueError("repositories must be a non-empty JSON list")
        if not isinstance(registries, list) or not registries:
            raise ValueError("registries must be a non-empty JSON list")
        if not isinstance(targets, Mapping) or not targets:
            raise ValueError("compose_targets must be a non-empty JSON object")
        repository_values = frozenset(_safe_repository(item) for item in repositories)
        registry_values = frozenset(_safe_registry(item) for item in registries)
        composed: dict[str, DeploymentTarget] = {}
        for name, raw_target in targets.items():
            if not isinstance(name, str) or _TARGET_NAME_RE.fullmatch(name) is None:
                raise ValueError("compose target names must be stable lowercase names")
            if not isinstance(raw_target, Mapping) or set(raw_target) != {
                "source_repository",
                "compose_file",
                "environment",
            }:
                raise ValueError(
                    "each compose target must contain exactly source_repository, "
                    "compose_file, and environment"
                )
            source_repository = _safe_repository(raw_target["source_repository"])
            if source_repository not in repository_values:
                raise ValueError("compose target source_repository must be allowlisted")
            environment = raw_target["environment"]
            if not isinstance(environment, Mapping):
                raise ValueError("compose target environment must be an object")
            environment_values: dict[str, str] = {}
            for env_name, env_value in environment.items():
                if (
                    not isinstance(env_name, str)
                    or _ENV_NAME_RE.fullmatch(env_name) is None
                ):
                    raise ValueError("compose environment names must be valid names")
                if not isinstance(env_value, str) or "\x00" in env_value:
                    raise ValueError("compose environment values must be strings")
                environment_values[env_name] = env_value
            composed[name] = DeploymentTarget(
                _compose_file_path(raw_target["compose_file"]),
                source_repository,
                MappingProxyType(environment_values),
            )
        return cls(
            repositories=repository_values,
            registries=registry_values,
            compose_targets=MappingProxyType(composed),
        )

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> OperationAllowlist:
        config_path = values.get("AGENTD_WORKER_OPERATIONS_CONFIG")
        if config_path:
            path = Path(config_path).expanduser().resolve()
            try:
                if path.stat().st_size > _MAX_CONFIG_BYTES:
                    raise ValueError("operations config exceeds the size limit")
                raw = json.loads(
                    path.read_text(encoding="utf-8"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_keys,
                )
            except (OSError, UnicodeError, TypeError, ValueError) as error:
                if isinstance(error, ValueError) and str(error).endswith(
                    "exceeds the size limit"
                ):
                    raise
                raise ValueError("operations config is not valid JSON") from error
            if not isinstance(raw, Mapping):
                raise ValueError("operations config must be a JSON object")
            return cls.from_mapping(raw, base_dir=path.parent)

        repositories = _csv_values(
            values.get("AGENTD_WORKER_REPOSITORIES"),
            "AGENTD_WORKER_REPOSITORIES",
            _safe_repository,
        )
        registries = _csv_values(
            values.get("AGENTD_WORKER_REGISTRIES"),
            "AGENTD_WORKER_REGISTRIES",
            _safe_registry,
        )
        targets_raw = values.get("AGENTD_WORKER_COMPOSE_TARGETS")
        if targets_raw is None:
            raise ValueError("AGENTD_WORKER_COMPOSE_TARGETS is required")
        try:
            targets = json.loads(
                targets_raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "AGENTD_WORKER_COMPOSE_TARGETS is not valid JSON"
            ) from error
        return cls.from_mapping(
            {
                "repositories": sorted(repositories),
                "registries": sorted(registries),
                "compose_targets": targets,
            },
            base_dir=Path.cwd(),
        )


@dataclass(frozen=True, slots=True)
class WorkerServeConfig:
    """Validated non-secret inputs for one worker daemon."""

    host: str
    port: int
    node_id: str
    session_epoch: str
    journal_path: Path
    psk_env_name: str = "AGENTD_WORKER_PSK"
    psk_file: Path | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None
    allow_insecure_loopback: bool = False
    operations_config: Path | None = None
    operation_cache_root: Path | None = None
    operation_state_root: Path | None = None

    def __post_init__(self) -> None:
        _required_text(self.host, "host")
        if not isinstance(self.port, int) or not 0 <= self.port <= 65_535:
            raise ValueError("worker port must be between 0 and 65535")
        _required_text(self.node_id, "node_id")
        _required_text(self.session_epoch, "session_epoch")
        if not isinstance(self.journal_path, Path):
            raise TypeError("journal_path must be a Path")
        if not _ENV_NAME_RE.fullmatch(self.psk_env_name):
            raise ValueError("psk_env_name must be a valid environment variable name")
        if self.psk_file is not None and self.psk_file == self.journal_path:
            raise ValueError("PSK file must not overwrite the journal")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("TLS certificate and key must be configured together")
        if self.tls_cert is None and not self.allow_insecure_loopback:
            raise ValueError("TLS certificate and key are required by default")
        if self.allow_insecure_loopback and self.host not in _LOOPBACK_HOSTS:
            raise ValueError("insecure worker transport is restricted to loopback")

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> WorkerServeConfig:
        host = values.get("AGENTD_WORKER_HOST", "0.0.0.0")
        try:
            port = int(values.get("AGENTD_WORKER_PORT", "8765"))
        except ValueError as error:
            raise ValueError("AGENTD_WORKER_PORT must be an integer") from error
        node_id = _required_text(values.get("AGENTD_WORKER_NODE_ID"), "node_id")
        session_epoch = _required_text(
            values.get("AGENTD_WORKER_SESSION_EPOCH"),
            "session_epoch",
        )
        journal_raw = values.get("AGENTD_WORKER_JOURNAL")
        if not journal_raw:
            raise ValueError("AGENTD_WORKER_JOURNAL is required")
        journal_path = Path(journal_raw).expanduser().resolve()
        psk_file_raw = values.get("AGENTD_WORKER_PSK_FILE")
        psk_env_name = values.get("AGENTD_WORKER_PSK_ENV", "AGENTD_WORKER_PSK")
        if not _ENV_NAME_RE.fullmatch(psk_env_name):
            raise ValueError("AGENTD_WORKER_PSK_ENV must name an environment variable")
        if psk_file_raw and psk_env_name in values:
            raise ValueError("configure the worker PSK from env or file, not both")
        if not psk_file_raw and psk_env_name not in values:
            raise ValueError("worker PSK must come from the configured env or file")
        if not psk_file_raw and len(values[psk_env_name].encode("utf-8")) < 32:
            raise ValueError("worker PSK must contain at least 32 bytes")
        allow_insecure = _parse_bool(
            values.get("AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK"),
            "AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK",
        )
        cert_raw = values.get("AGENTD_WORKER_TLS_CERT")
        key_raw = values.get("AGENTD_WORKER_TLS_KEY")
        if bool(cert_raw) != bool(key_raw):
            raise ValueError("AGENTD_WORKER_TLS_CERT and TLS_KEY must be paired")
        journal_parent = journal_path.parent
        return cls(
            host=host,
            port=port,
            node_id=node_id,
            session_epoch=session_epoch,
            journal_path=journal_path,
            psk_env_name=psk_env_name,
            psk_file=(
                Path(os.path.abspath(Path(psk_file_raw).expanduser()))
                if psk_file_raw
                else None
            ),
            tls_cert=(Path(cert_raw).expanduser().resolve() if cert_raw else None),
            tls_key=(Path(key_raw).expanduser().resolve() if key_raw else None),
            allow_insecure_loopback=allow_insecure,
            operations_config=(
                Path(values["AGENTD_WORKER_OPERATIONS_CONFIG"]).expanduser().resolve()
                if values.get("AGENTD_WORKER_OPERATIONS_CONFIG")
                else None
            ),
            operation_cache_root=(
                Path(
                    values.get(
                        "AGENTD_WORKER_CACHE_ROOT",
                        str(journal_parent / "cache"),
                    )
                )
                .expanduser()
                .resolve()
            ),
            operation_state_root=(
                Path(
                    values.get(
                        "AGENTD_WORKER_OPERATION_STATE_ROOT",
                        str(journal_parent / "operations"),
                    )
                )
                .expanduser()
                .resolve()
            ),
        )

    def load_secret(self, values: Mapping[str, str]) -> bytes:
        if self.psk_file is not None:
            try:
                if self.psk_file.is_symlink():
                    raise ValueError("PSK file must not be a symlink")
                mode = self.psk_file.stat().st_mode & 0o777
                if mode != 0o600:
                    raise ValueError("PSK file must be mode 0600")
                secret = self.psk_file.read_bytes()
            except OSError as error:
                raise ValueError("worker PSK file cannot be read") from error
        else:
            raw = values.get(self.psk_env_name)
            if raw is None:
                raise ValueError("worker PSK environment variable is not set")
            secret = raw.encode("utf-8")
        if len(secret) < 32:
            raise ValueError("worker PSK must contain at least 32 bytes")
        return secret

    def tls_context(self) -> ssl.SSLContext | None:
        if self.tls_cert is None or self.tls_key is None:
            if not self.allow_insecure_loopback:
                raise ValueError("TLS certificate and key are required by default")
            return None
        if not self.tls_cert.is_file() or not self.tls_key.is_file():
            raise ValueError("configured TLS certificate and key must be files")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(self.tls_cert, self.tls_key)
        except (OSError, ssl.SSLError) as error:
            raise ValueError(
                "configured TLS certificate/key cannot be loaded"
            ) from error
        return context

    def operation_allowlist(self, values: Mapping[str, str]) -> OperationAllowlist:
        if self.operations_config is not None:
            merged = dict(values)
            merged["AGENTD_WORKER_OPERATIONS_CONFIG"] = str(self.operations_config)
            return OperationAllowlist.from_environment(merged)
        return OperationAllowlist.from_environment(values)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("operations config contains duplicate keys")
        result[key] = value
    return result


def create_worker_server(
    config: WorkerServeConfig,
    *,
    values: Mapping[str, str] | None = None,
    extra_drivers: Iterable[HarnessDriver] = (),
) -> WorkerServer:
    """Compose a WorkerServer with the allowlisted typed operation driver."""

    environment = os.environ if values is None else values
    secret = config.load_secret(environment)
    if config.psk_file is None:
        # The daemon keeps the key in memory after startup.  Do not let the
        # authentication secret leak into Git, Docker, or Compose subprocesses
        # through their inherited environment.
        os.environ.pop(config.psk_env_name, None)
    allowlist = config.operation_allowlist(environment)
    cache_root = config.operation_cache_root or (config.journal_path.parent / "cache")
    state_root = config.operation_state_root or (
        config.journal_path.parent / "operations"
    )
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(cache_root, 0o700)
        os.chmod(state_root, 0o700)
    operation_driver = OperationHarnessDriver(
        DockerImageBuilder(allowlist.registries, cache_root),
        DockerComposeDeployer(
            allowlist.compose_targets,
            state_root,
            allowlist.repositories,
            allowlist.registries,
        ),
        repository_allowlist=allowlist.repositories,
    )
    from agentd.workers.journal import OperationJournal

    journal = OperationJournal(
        config.journal_path,
        node_id=config.node_id,
        session_epoch=config.session_epoch,
    )
    try:
        return WorkerServer(
            config.host,
            config.port,
            node_id=config.node_id,
            session_epoch=config.session_epoch,
            secret=secret,
            drivers=(operation_driver, *tuple(extra_drivers)),
            journal=journal,
            ssl_context=config.tls_context(),
            allow_insecure_loopback=config.allow_insecure_loopback,
        )
    except BaseException:
        journal.close()
        raise


async def run_worker_server(
    config: WorkerServeConfig,
    *,
    values: Mapping[str, str] | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    """Run one worker until SIGINT/SIGTERM or an injected stop event."""

    server = create_worker_server(config, values=values)
    stop_event = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(received, stop_event.set)
    serving: asyncio.Task[None] | None = None
    stopped: asyncio.Task[bool] | None = None
    try:
        await server.start()
        serving = asyncio.create_task(server.serve_forever())
        stopped = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            (serving, stopped),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if serving in done:
            exception = serving.exception()
            if exception is not None:
                raise exception
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if serving is not None and not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        if stopped is not None and not stopped.done():
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
        await server.close()
    return 0


__all__ = [
    "OperationAllowlist",
    "WorkerServeConfig",
    "create_worker_server",
    "run_worker_server",
]
