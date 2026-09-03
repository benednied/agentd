"""Trusted, typed worker operations.

This module is deliberately small.  It is the worker-side boundary for build and
deployment jobs: callers provide domain objects, while this module owns the
allowlists, immutable references, and the only subprocess entry point.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeGuard, cast
from urllib.parse import urlsplit
from uuid import uuid4

from agentd.domain.enums import ArtifactKind, RunOutcome
from agentd.domain.models import (
    ArtifactRef,
    BuildImageOperation,
    DeployImageOperation,
    ExecutionContract,
    HarnessCapabilities,
    JsonValue,
    ProducedArtifact,
    RunHandle,
    RunResult,
)
from agentd.workers.protocol import ARTIFACT_VERIFICATION_FEATURE


class OperationError(RuntimeError):
    """Raised when a typed operation cannot be safely completed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


# Keep the worker's command boundary explicit.  In particular, do not derive
# this from prefixes such as ``DOCKER_`` or ``LC_``: either prefix can contain
# values that alter command execution or disclose credentials.  ``HOME`` and
# ``DOCKER_CONFIG`` are intentionally retained so Docker can use the
# operator-managed credential/configuration files.  No SSH variables are
# included; Git operations are restricted to HTTPS and local file transports.
_PROCESS_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DOCKER_CONFIG",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_TLS",
    "DOCKER_DEFAULT_PLATFORM",
    "BUILDX_CONFIG",
    "BUILDX_BUILDER",
    "BUILDKIT_PROGRESS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_COLLATE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
)


def _process_environment() -> dict[str, str]:
    """Return only the reviewed host environment needed by Git and Docker."""

    return {
        name: value
        for name in _PROCESS_ENVIRONMENT_ALLOWLIST
        if (value := os.environ.get(name)) is not None
    } | {"GIT_TERMINAL_PROMPT": "0"}


def _git_environment() -> dict[str, str]:
    """Isolate Git from host-wide URL rewrites and command configuration."""

    return _process_environment() | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def _redact(value: str) -> str:
    value = re.sub(
        r"(?i)(password|token|secret|authorization)=([^\s]+)", r"\1=<redacted>", value
    )
    return value[:4096]


@dataclass(frozen=True, slots=True)
class SubprocessCommandRunner:
    """Bounded, timeout-aware subprocess execution without a shell."""

    timeout_seconds: float = 900
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("command limits must be positive")

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        args = tuple(argv)
        if not args or any(
            not isinstance(item, str) or not item or "\0" in item for item in args
        ):
            raise OperationError("command arguments are invalid")
        # ``None`` must not mean inherit the worker's complete environment.
        # Callers with an explicit operator-trusted environment (Compose
        # targets) still pass it below and retain those values verbatim.
        process_environment = (
            _process_environment() if environment is None else dict(environment)
        )
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd is not None else None,
            env=process_environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        limit = timeout if timeout is not None else self.timeout_seconds

        async def read_bounded(stream: asyncio.StreamReader) -> bytes:
            chunks: list[bytes] = []
            total = 0
            while chunk := await stream.read(65_536):
                total += len(chunk)
                if total > self.max_output_bytes:
                    raise OperationError("command output exceeded the configured limit")
                chunks.append(chunk)
            return b"".join(chunks)

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    # PIPE guarantees both streams, but asyncio's Process type
                    # cannot correlate those arguments with its attributes.
                    read_bounded(cast(asyncio.StreamReader, process.stdout)),
                    read_bounded(cast(asyncio.StreamReader, process.stderr)),
                ),
                timeout=limit,
            )
            await asyncio.wait_for(process.wait(), timeout=limit)
        except BaseException:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        result = CommandResult(process.returncode or 0, stdout, stderr)
        if result.returncode != 0:
            detail = _redact(result.stderr.decode("utf-8", "replace").strip())
            raise OperationError(
                f"command failed with exit code {result.returncode}"
                + (f": {detail}" if detail else "")
            )
        return result


@dataclass(frozen=True, slots=True)
class GitWorkspace:
    source_repository: str
    commit: ArtifactRef
    path: Path
    mirror: Path


@dataclass(slots=True)
class GitRepositoryCache:
    """Allowlisted bare mirrors and detached, confined worktrees."""

    root: Path
    repositories: frozenset[str]
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        self.repositories = frozenset(self.repositories)
        if not self.repositories:
            raise ValueError("at least one repository must be allowlisted")
        for directory, label in (
            (self.root / "mirrors", "Mirror root"),
            (self.root / "worktrees", "Worktree root"),
        ):
            if directory.is_symlink():
                raise OperationError(f"{label} must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise OperationError(f"{label} must be a real directory")

    async def checkout(
        self, source_repository: str, commit: ArtifactRef
    ) -> GitWorkspace:
        self._check_repository(source_repository)
        self._check_commit(commit)
        key = hashlib.sha256(source_repository.encode()).hexdigest()
        mirror = self.root / "mirrors" / key
        mirror_root = self.root / "mirrors"
        worktree_root = self.root / "worktrees"
        environment = _git_environment()
        self._confined(mirror, mirror_root, "Mirror path")
        if mirror.exists():
            if not mirror.is_dir():
                raise OperationError("Mirror path must be a directory")
            await self.runner.run(
                ("git", "-C", str(mirror), "fetch", "--prune", "origin"),
                environment=environment,
            )
        else:
            await self.runner.run(
                ("git", "clone", "--bare", source_repository, str(mirror)),
                environment=environment,
            )
        await self.runner.run(
            (
                "git",
                "-C",
                str(mirror),
                "cat-file",
                "-e",
                f"{commit.value}^{{commit}}",
            ),
            environment=environment,
        )
        verified = await self.runner.run(
            (
                "git",
                "-C",
                str(mirror),
                "rev-parse",
                f"{commit.value}^{{commit}}",
            ),
            environment=environment,
        )
        actual = verified.stdout.decode("ascii", "ignore").strip().lower()
        if actual != commit.value:
            raise OperationError("Git commit verification returned another object")
        worktree = self.root / "worktrees" / f"{key}-{commit.value}"
        self._confined(worktree, worktree_root, "Worktree path")
        if not worktree.exists():
            await self.runner.run(
                (
                    "git",
                    "-C",
                    str(mirror),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    commit.value,
                ),
                environment=environment,
            )
        elif not worktree.is_dir():
            raise OperationError("Worktree path must be a directory")
        worktree_head = await self.runner.run(
            (
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "HEAD^{commit}",
            ),
            environment=environment,
        )
        if worktree_head.stdout.decode("ascii", "ignore").strip().lower() != (
            commit.value
        ):
            raise OperationError("Git worktree is not at the requested commit")
        worktree_status = await self.runner.run(
            (
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            environment=environment,
        )
        if worktree_status.stdout.strip():
            raise OperationError("Git worktree contains changes outside the commit")
        return GitWorkspace(source_repository, commit, worktree, mirror)

    async def cleanup(self, workspace: GitWorkspace) -> None:
        self._check_repository(workspace.source_repository)
        self._confined(workspace.path, self.root / "worktrees", "Worktree path")
        self._confined(workspace.mirror, self.root / "mirrors", "Mirror path")
        if workspace.path.exists():
            if not workspace.path.is_dir():
                raise OperationError("Worktree path must be a directory")
            try:
                await self.runner.run(
                    (
                        "git",
                        "-C",
                        str(workspace.mirror),
                        "worktree",
                        "remove",
                        "--force",
                        str(workspace.path),
                    ),
                    environment=_git_environment(),
                )
            finally:
                shutil.rmtree(workspace.path, ignore_errors=True)

    def _check_repository(self, source: str) -> None:
        _validate_repository_transport(source)
        if source not in self.repositories:
            raise OperationError("source repository is not allowlisted")

    @staticmethod
    def _check_commit(commit: ArtifactRef) -> None:
        if commit.kind is not ArtifactKind.GIT_COMMIT or len(commit.value) not in (
            40,
            64,
        ):
            raise OperationError("operation requires a full Git commit SHA")

    def _confined(self, path: Path, root: Path, label: str) -> None:
        if path.is_symlink():
            raise OperationError(f"{label} must not be a symlink")
        if root.is_symlink():
            raise OperationError(f"{label} root must not be a symlink")
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as error:
            raise OperationError(f"{label} escapes the cache") from error


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_OCI_IMAGE_REF_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
)
_GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SCP_STYLE_RE = re.compile(r"[^/\s]+@[^:\s]+:")
_DEPLOYMENT_STATE_FIELDS = frozenset({"image", "config", "config_digest"})
_DEPLOYMENT_PENDING_FIELDS = frozenset({"previous", "desired"})


def _validate_repository_transport(source: str) -> None:
    """Reject unapproved Git transports before any subprocess is launched."""

    if source.startswith("ssh://") or _SCP_STYLE_RE.match(source):
        raise OperationError("SSH/SCP repository transports are not allowed")
    if "://" not in source:
        return
    parsed = urlsplit(source)
    if parsed.scheme not in {"https", "file"}:
        raise OperationError("repository URL must use HTTPS or a local file URL")
    if parsed.username is not None or parsed.password is not None:
        raise OperationError("repository URL cannot contain credentials")
    if parsed.scheme == "https" and not parsed.hostname:
        raise OperationError("repository HTTPS URL must include a host")
    if parsed.scheme == "file" and not parsed.path:
        raise OperationError("repository file URL must include a path")


def _digest_from_metadata(value: object) -> str | None:
    """Read only Buildx's authoritative output-manifest digest fields.

    Build metadata may also contain source, config, or attestation digests. A
    recursive "first sha256 wins" search can therefore publish an older,
    unrelated registry object as this build's output.
    """

    if not isinstance(value, Mapping):
        return None
    candidates: list[str] = []
    direct = value.get("containerimage.digest")
    if direct is not None:
        if not isinstance(direct, str) or _DIGEST_RE.fullmatch(direct) is None:
            raise OperationError("Buildx output digest is malformed")
        candidates.append(direct)
    descriptor = value.get("containerimage.descriptor")
    if descriptor is not None:
        if not isinstance(descriptor, Mapping):
            raise OperationError("Buildx output descriptor is malformed")
        descriptor_digest = descriptor.get("digest")
        if (
            not isinstance(descriptor_digest, str)
            or _DIGEST_RE.fullmatch(descriptor_digest) is None
        ):
            raise OperationError("Buildx output descriptor digest is malformed")
        candidates.append(descriptor_digest)
    if not candidates:
        return None
    if len(set(candidates)) != 1:
        raise OperationError("Buildx output digest fields disagree")
    return candidates[0]


def _is_canonical_oci_image(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _OCI_IMAGE_REF_RE.fullmatch(value) is not None


def _canonical_json_digest(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OperationError("docker compose config is not canonical JSON") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


@dataclass(slots=True)
class DockerImageBuilder:
    registry_allowlist: frozenset[str]
    cache_root: Path
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)

    def __post_init__(self) -> None:
        self.registry_allowlist = frozenset(self.registry_allowlist)
        self.cache_root = self.cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    async def build(
        self, operation: BuildImageOperation, workspace: GitWorkspace
    ) -> ProducedArtifact:
        if operation.source_input != workspace.commit:
            raise OperationError(
                "build source commit does not match checked out commit"
            )
        self._check_registry(operation.registry_repository)
        provenance: dict[str, JsonValue] = {
            "source_repository": operation.source_repository,
            "source_commit": workspace.commit.value,
            "registry_repository": operation.registry_repository,
            "output_name": operation.output_name,
            "context": operation.context,
            "dockerfile": operation.dockerfile,
            "platforms": list(operation.platforms),
        }
        cache_key = hashlib.sha256(
            json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache = self.cache_root / f"{cache_key}.json"
        if cache.exists():
            try:
                cached = ProducedArtifact.from_dict(json.loads(cache.read_text()))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise OperationError("cached image record is invalid") from error
            if not self._cache_matches(cached, provenance):
                raise OperationError("cached image provenance does not match operation")
            await self._verify_registry_digest(cached.ref)
            return cached
        metadata = self.cache_root / f"{cache_key}.metadata.json"
        try:
            metadata.unlink(missing_ok=True)
        except OSError as error:
            raise OperationError("stale build metadata could not be removed") from error
        tag = f"build-{workspace.commit.value}"
        image = f"{operation.registry_repository}:{tag}"
        args = [
            "docker",
            "buildx",
            "build",
            "--push",
            "--metadata-file",
            str(metadata),
            "--file",
            operation.dockerfile,
        ]
        if operation.platforms:
            args.extend(("--platform", ",".join(operation.platforms)))
        args.extend(("--tag", image, operation.context))
        await self.runner.run(
            tuple(args), cwd=workspace.path, environment=_process_environment()
        )
        if metadata.is_symlink() or not metadata.is_file():
            raise OperationError("build did not produce a regular metadata file")
        try:
            raw = json.loads(
                metadata.read_text(),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise OperationError("build metadata is not valid JSON") from error
        digest = _digest_from_metadata(raw)
        if digest is None:
            raise OperationError("build metadata did not contain an exact image digest")
        ref = ArtifactRef(
            ArtifactKind.OCI_IMAGE, f"{operation.registry_repository}@{digest}"
        )
        await self._verify_registry_digest(ref)
        artifact = ProducedArtifact(
            operation.output_name,
            ref,
            metadata={"provenance": provenance},
        )
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(artifact.to_dict(), sort_keys=True))
        temporary.replace(cache)
        return artifact

    async def _verify_registry_digest(self, ref: ArtifactRef) -> None:
        """Verify a cached or freshly built digest against the registry."""

        verified = await self.runner.run(
            ("docker", "buildx", "imagetools", "inspect", ref.value),
            environment=_process_environment(),
        )
        inspected = verified.stdout.decode("utf-8", "replace")
        digest = ref.value.rsplit("@", 1)[1]
        if not self._inspect_has_digest(inspected, digest):
            raise OperationError(
                "registry verification did not return the requested digest"
            )

    def _check_registry(self, repository: str) -> None:
        if repository not in self.registry_allowlist:
            raise OperationError("registry repository is not allowlisted")

    @staticmethod
    def _cache_matches(
        artifact: ProducedArtifact, provenance: Mapping[str, JsonValue]
    ) -> bool:
        if artifact.ref.kind is not ArtifactKind.OCI_IMAGE:
            return False
        expected_repository = str(provenance["registry_repository"])
        if not artifact.ref.value.startswith(expected_repository + "@sha256:"):
            return False
        return artifact.metadata.get("provenance") == dict(provenance)

    @staticmethod
    def _inspect_has_digest(output: str, expected_digest: str) -> bool:
        reported = {
            match.group(1)
            for match in re.finditer(
                r"(?im)^\s*Digest:\s*(sha256:[0-9a-f]{64})\s*$", output
            )
        }
        return expected_digest in reported


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    """Allowlisted deployment configuration stored in an immutable Git repo."""

    compose_file: Path
    source_repository: str
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.compose_file, Path):
            raise TypeError("compose_file must be a Path")
        if (
            self.compose_file.is_absolute()
            or not self.compose_file.parts
            or any(part in {"", ".", ".."} for part in self.compose_file.parts)
            or "\0" in str(self.compose_file)
        ):
            raise ValueError("compose_file must be a confined relative path")
        _validate_repository_transport(self.source_repository)


@dataclass(slots=True)
class DockerComposeDeployer:
    targets: Mapping[str, DeploymentTarget]
    state_root: Path
    repository_allowlist: frozenset[str]
    registry_allowlist: frozenset[str]
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)

    def __post_init__(self) -> None:
        self.targets = dict(self.targets)
        self.repository_allowlist = frozenset(self.repository_allowlist)
        self.registry_allowlist = frozenset(self.registry_allowlist)
        if not self.repository_allowlist:
            raise ValueError("at least one config repository must be allowlisted")
        if not self.registry_allowlist:
            raise ValueError("at least one image registry must be allowlisted")
        for target in self.targets.values():
            if target.source_repository not in self.repository_allowlist:
                raise ValueError(
                    "deployment target repository must be explicitly allowlisted"
                )
        self.state_root = self.state_root.expanduser().resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)

    async def deploy(self, operation: DeployImageOperation) -> None:
        if operation.target not in self.targets:
            raise OperationError("deployment target is not allowlisted")
        if (
            not isinstance(operation.image_input, ArtifactRef)
            or operation.image_input.kind is not ArtifactKind.OCI_IMAGE
            or not _is_canonical_oci_image(operation.image_input.value)
        ):
            raise OperationError("deployment requires an immutable image digest")
        image_repository = operation.image_input.value.rsplit("@", 1)[0]
        if image_repository not in self.registry_allowlist:
            raise OperationError("deployment image registry is not allowlisted")
        target = self.targets[operation.target]
        state = self.state_root / f"{operation.target}-{operation.deployment_name}.json"
        pending = self._pending_path(state)
        await self._recover_pending(
            state,
            pending,
            target,
            operation.deployment_name,
        )
        compose = await self._prepare_compose_file(
            target,
            operation.config_revision,
        )
        previous = self._read_state(state)
        environment = self._environment(
            target,
            operation.image_input.value,
            operation.config_revision.value,
            operation.deployment_name,
        )
        config_digest = await self._resolve_config(
            compose,
            operation.deployment_name,
            environment,
            expected_image=operation.image_input.value,
            expected_config=operation.config_revision.value,
        )
        if (
            previous is not None
            and previous["image"] == operation.image_input.value
            and previous["config"] == operation.config_revision.value
            and previous["config_digest"] == config_digest
        ):
            return
        desired = {
            "image": operation.image_input.value,
            "config": operation.config_revision.value,
            "config_digest": config_digest,
        }
        try:
            self._write_pending(
                pending,
                {
                    "previous": previous,
                    "desired": desired,
                },
            )
        except Exception as error:
            raise OperationError("deployment intent could not be persisted") from error
        args = self._compose_up_args(compose, operation.deployment_name)
        try:
            await self.runner.run(args, cwd=compose.parent, environment=environment)
        except asyncio.CancelledError:
            rollback_error = await self._attempt_rollback(
                operation.deployment_name,
                target,
                previous,
                compose=compose,
                desired=desired,
            )
            if rollback_error is None:
                with suppress(Exception):
                    self._remove_pending(pending)
            raise
        except Exception as error:
            rollback_error = await self._attempt_rollback(
                operation.deployment_name,
                target,
                previous,
                compose=compose,
                desired=desired,
            )
            if rollback_error is not None:
                raise OperationError(
                    "deployment failed and rollback failed; pending marker retained"
                ) from rollback_error
            with suppress(Exception):
                self._remove_pending(pending)
            raise OperationError(
                "deployment failed and rollback was attempted"
            ) from error
        try:
            self._write_state(state, desired)
        except Exception as error:
            # The up side effect is already real. First put the durable marker
            # back at the known previous value; only then roll the external
            # deployment back. This ordering keeps recovery deterministic even
            # when the failed write made the desired state briefly visible.
            restore_error = await self._attempt_restore_state(state, previous)
            if restore_error is not None:
                raise OperationError(
                    "deployment state persistence failed; pending marker retained"
                ) from restore_error
            rollback_error = await self._attempt_rollback(
                operation.deployment_name,
                target,
                previous,
                compose=compose,
                desired=desired,
            )
            if rollback_error is not None:
                raise OperationError(
                    "deployment state persistence failed and rollback failed; "
                    "pending marker retained"
                ) from rollback_error
            with suppress(Exception):
                self._remove_pending(pending)
            raise OperationError(
                "deployment state persistence failed and rollback was attempted"
            ) from error
        # Main state is committed. If cleanup itself fails, retaining the
        # marker is safe: the next invocation sees main == desired and retries
        # only this finalization step. The deployment remains successful.
        with suppress(Exception):
            self._remove_pending(pending)

    @staticmethod
    def _pending_path(state: Path) -> Path:
        return state.with_suffix(".pending.json")

    @staticmethod
    def _validate_state_marker(value: object, *, label: str) -> dict[str, str]:
        if not isinstance(value, dict) or set(value) != _DEPLOYMENT_STATE_FIELDS:
            raise OperationError(f"{label} fields are invalid")
        image = value["image"]
        config = value["config"]
        config_digest = value["config_digest"]
        if (
            not _is_canonical_oci_image(image)
            or not isinstance(config, str)
            or _GIT_SHA_RE.fullmatch(config) is None
            or not isinstance(config_digest, str)
            or _DIGEST_RE.fullmatch(config_digest) is None
        ):
            raise OperationError(f"{label} values are invalid")
        return {
            "image": image,
            "config": config,
            "config_digest": config_digest,
        }

    @classmethod
    def _decode_json_file(cls, path: Path, *, label: str) -> object:
        try:
            return json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise OperationError(f"{label} is invalid") from error

    @classmethod
    def _read_state(cls, state: Path) -> dict[str, str] | None:
        if state.is_symlink():
            raise OperationError("deployment state is invalid")
        if not state.exists():
            return None
        if not state.is_file():
            raise OperationError("deployment state is invalid")
        raw = cls._decode_json_file(state, label="deployment state")
        try:
            return cls._validate_state_marker(raw, label="deployment state")
        except OperationError as error:
            raise OperationError("deployment state is invalid") from error

    @classmethod
    def _read_pending(
        cls, pending: Path
    ) -> tuple[dict[str, str] | None, dict[str, str]] | None:
        if pending.is_symlink():
            raise OperationError("deployment pending marker is invalid")
        if not pending.exists():
            return None
        if not pending.is_file():
            raise OperationError("deployment pending marker is invalid")
        raw = cls._decode_json_file(pending, label="deployment pending marker")
        if not isinstance(raw, dict) or set(raw) != _DEPLOYMENT_PENDING_FIELDS:
            raise OperationError("deployment pending marker fields are invalid")
        previous = raw["previous"]
        if previous is not None:
            previous = cls._validate_state_marker(
                previous,
                label="deployment pending previous state",
            )
        desired = cls._validate_state_marker(
            raw["desired"],
            label="deployment pending desired state",
        )
        return previous, desired

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_write_json(cls, path: Path, value: Mapping[str, object]) -> None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OperationError("deployment marker path is invalid")
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        except Exception:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                temporary.unlink()
            raise

    @classmethod
    def _write_pending(
        cls,
        pending: Path,
        value: Mapping[str, object],
    ) -> None:
        cls._atomic_write_json(pending, value)

    @classmethod
    def _write_state(cls, state: Path, value: Mapping[str, object]) -> None:
        cls._atomic_write_json(state, value)

    @classmethod
    def _remove_pending(cls, pending: Path) -> None:
        if pending.is_symlink() or (pending.exists() and not pending.is_file()):
            raise OperationError("deployment pending marker is invalid")
        try:
            pending.unlink()
        except FileNotFoundError:
            return
        cls._fsync_directory(pending.parent)

    @classmethod
    def _remove_state(cls, state: Path) -> None:
        if state.is_symlink() or (state.exists() and not state.is_file()):
            raise OperationError("deployment state is invalid")
        try:
            state.unlink()
        except FileNotFoundError:
            return
        cls._fsync_directory(state.parent)

    @classmethod
    async def _attempt_restore_state(
        cls,
        state: Path,
        previous: dict[str, str] | None,
    ) -> BaseException | None:
        try:
            if previous is None:
                cls._remove_state(state)
            else:
                cls._write_state(state, previous)
        except BaseException as error:
            return error
        return None

    async def _attempt_rollback(
        self,
        deployment_name: str,
        target: DeploymentTarget,
        previous: Mapping[str, str] | None,
        *,
        compose: Path,
        desired: Mapping[str, str],
    ) -> BaseException | None:
        try:
            await self._rollback(
                deployment_name,
                target,
                previous,
                compose=compose,
                desired=desired,
            )
        except BaseException as error:
            return error
        return None

    async def _recover_pending(
        self,
        state: Path,
        pending: Path,
        target: DeploymentTarget,
        deployment_name: str,
    ) -> None:
        marker = self._read_pending(pending)
        if marker is None:
            return
        previous, desired = marker
        current = self._read_state(state)
        if current == desired:
            with suppress(Exception):
                self._remove_pending(pending)
            return
        if current != previous:
            raise OperationError(
                "deployment pending marker does not match durable state"
            )
        try:
            compose: Path | None = None
            if previous is None:
                compose = await self._prepare_compose_file(
                    target,
                    ArtifactRef(ArtifactKind.GIT_COMMIT, desired["config"]),
                )
            else:
                compose = await self._prepare_compose_file(
                    target,
                    ArtifactRef(ArtifactKind.GIT_COMMIT, previous["config"]),
                )
            await self._rollback(
                deployment_name,
                target,
                previous,
                compose=compose,
                desired=desired,
            )
            with suppress(Exception):
                self._remove_pending(pending)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise OperationError(
                "deployment pending recovery failed; marker retained"
            ) from error

    async def _rollback(
        self,
        deployment_name: str,
        target: DeploymentTarget,
        previous: Mapping[str, str] | None,
        *,
        compose: Path | None,
        desired: Mapping[str, str],
    ) -> None:
        """Restore the exact previous external state, or no deployment."""

        if previous is None:
            if compose is None:
                raise OperationError("rollback lacks the desired Compose file")
            rollback_environment = self._environment(
                target,
                desired["image"],
                desired["config"],
                deployment_name,
            )
            rollback_digest = await self._resolve_config(
                compose,
                deployment_name,
                rollback_environment,
                expected_image=desired["image"],
                expected_config=desired["config"],
            )
            if rollback_digest != desired["config_digest"]:
                raise OperationError(
                    "rollback resolved config does not match pending state"
                )
            await self.runner.run(
                self._compose_down_args(compose, deployment_name),
                cwd=compose.parent,
                environment=rollback_environment,
            )
            return

        old_digest = previous["image"]
        old_config = previous["config"]
        old_compose = await self._prepare_compose_file(
            target,
            ArtifactRef(ArtifactKind.GIT_COMMIT, old_config),
        )
        rollback_environment = self._environment(
            target,
            old_digest,
            old_config,
            deployment_name,
        )
        rollback_digest = await self._resolve_config(
            old_compose,
            deployment_name,
            rollback_environment,
            expected_image=old_digest,
            expected_config=old_config,
        )
        if rollback_digest != previous["config_digest"]:
            raise OperationError(
                "rollback resolved config does not match previous state"
            )
        await self.runner.run(
            self._compose_up_args(old_compose, deployment_name),
            cwd=old_compose.parent,
            environment=rollback_environment,
        )

    async def _prepare_compose_file(
        self,
        target: DeploymentTarget,
        config_revision: ArtifactRef,
    ) -> Path:
        """Materialize the exact config commit in a stable detached worktree."""

        cache = GitRepositoryCache(
            self.state_root / "config-cache",
            self.repository_allowlist,
            self.runner,
        )
        workspace = await cache.checkout(target.source_repository, config_revision)
        compose = workspace.path / target.compose_file
        if compose.is_symlink() or not compose.is_file():
            raise OperationError(
                "compose_file is not a regular file in the requested config commit"
            )
        try:
            resolved = compose.resolve(strict=True)
            resolved.relative_to(workspace.path.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise OperationError("compose_file escapes the config worktree") from error
        return resolved

    @staticmethod
    def _environment(
        target: DeploymentTarget,
        image: str,
        config_revision: str,
        deployment_name: str,
    ) -> dict[str, str]:
        return {
            **_process_environment(),
            **dict(target.environment),
            # Operation-controlled values are applied after target values so a
            # target cannot redirect the compose file to another artifact.
            "AGENTD_IMAGE": image,
            "AGENTD_IMAGE_DIGEST": image,
            "AGENTD_CONFIG_REVISION": config_revision,
            "AGENTD_DEPLOYMENT_NAME": deployment_name,
        }

    async def _resolve_config(
        self,
        compose: Path,
        deployment_name: str,
        environment: Mapping[str, str],
        *,
        expected_image: str,
        expected_config: str,
    ) -> str:
        result = await self.runner.run(
            (
                "docker",
                "compose",
                "--project-name",
                deployment_name,
                "-f",
                str(compose),
                "config",
                "--format",
                "json",
            ),
            cwd=compose.parent,
            environment=environment,
        )
        try:
            resolved = json.loads(
                result.stdout.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise OperationError("docker compose config is not valid JSON") from error
        if not isinstance(resolved, dict):
            raise OperationError("docker compose config must be a JSON object")
        services = resolved.get("services")
        if not isinstance(services, Mapping) or not services:
            raise OperationError(
                "docker compose config must contain a non-empty services object"
            )

        matching_service = False
        matching_revision = False
        for name, service in services.items():
            if not isinstance(service, Mapping):
                raise OperationError(f"compose service {name!r} must be an object")
            image = service.get("image")
            if not _is_canonical_oci_image(image):
                raise OperationError(
                    f"compose service {name!r} image must use an OCI digest"
                )
            image_repository = image.rsplit("@", 1)[0]
            if image_repository not in self.registry_allowlist:
                raise OperationError(
                    f"compose service {name!r} image registry is not allowlisted"
                )
            if image != expected_image:
                continue
            matching_service = True
            service_environment = service.get("environment", {})
            service_labels = service.get("labels", {})
            if not isinstance(service_environment, Mapping):
                raise OperationError(
                    f"compose service {name!r} environment must be an object"
                )
            if not isinstance(service_labels, Mapping):
                raise OperationError(
                    f"compose service {name!r} labels must be an object"
                )
            if (
                service_environment.get("AGENTD_CONFIG_REVISION") == expected_config
                or service_labels.get("agentd.config-revision") == expected_config
            ):
                matching_revision = True

        if not matching_service:
            raise OperationError(
                "docker compose config does not consume the requested image"
            )
        if not matching_revision:
            raise OperationError(
                "docker compose config does not consume the requested config revision"
            )
        return _canonical_json_digest(resolved)

    @staticmethod
    def _compose_up_args(compose: Path, deployment_name: str) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            deployment_name,
            "-f",
            str(compose),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "always",
        )

    @staticmethod
    def _compose_down_args(compose: Path, deployment_name: str) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            deployment_name,
            "-f",
            str(compose),
            "down",
        )


@dataclass(slots=True)
class _OperationRun:
    task: asyncio.Task[RunResult]
    result: RunResult | None = None


@dataclass(slots=True)
class _OperationLockState:
    lock: asyncio.Lock
    users: int = 0


class OperationHarnessDriver:
    """Harness-shaped adapter that executes only typed build/deploy operations."""

    def __init__(
        self,
        builder: DockerImageBuilder,
        deployer: DockerComposeDeployer,
        *,
        repository_allowlist: Iterable[str] | None = None,
    ) -> None:
        self._builder = builder
        self._deployer = deployer
        self._repository_allowlist = (
            None if repository_allowlist is None else frozenset(repository_allowlist)
        )
        if self._repository_allowlist is not None and not self._repository_allowlist:
            raise ValueError("repository allowlist must not be empty")
        self._runs: dict[str, _OperationRun] = {}
        self._operation_locks: dict[str, _OperationLockState] = {}

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

    async def start(self, execution: ExecutionContract) -> RunHandle:
        if execution.operation is None:
            raise OperationError("operation driver requires a typed operation")
        run_id = f"operation-{uuid4()}"
        task = asyncio.create_task(self._execute(run_id, execution))
        self._runs[run_id] = _OperationRun(task)
        return RunHandle(id=run_id, driver="operations")

    async def steer(self, run: RunHandle, instruction: str) -> None:
        del run, instruction
        raise OperationError("typed operations do not support model steering")

    async def interrupt(self, run: RunHandle) -> None:
        await self.cancel(run)

    async def cancel(self, run: RunHandle) -> None:
        operation = self._runs.get(run.id)
        if operation is None:
            return
        if operation.task.done():
            operation.result = await self._result_from_task(operation)
            return
        operation.task.cancel()
        operation.result = await self._result_from_task(operation)

    async def status(self, run: RunHandle) -> dict[str, object]:
        operation = self._runs.get(run.id)
        if operation is None:
            return {"known": False, "terminal": False, "result": None}
        if operation.result is None and operation.task.done():
            operation.result = await self._result_from_task(operation)
        result = operation.result
        return {
            "known": True,
            "terminal": result is not None,
            "result": result.to_dict() if result is not None else None,
        }

    async def collect(self, run: RunHandle) -> RunResult:
        operation = self._runs.get(run.id)
        if operation is None:
            raise OperationError("unknown operation run")
        if operation.result is None:
            operation.result = await self._result_from_task(operation)
        return operation.result

    @staticmethod
    async def _result_from_task(operation: _OperationRun) -> RunResult:
        try:
            return await operation.task
        except asyncio.CancelledError:
            return RunResult(RunOutcome.CANCELLED, "operation cancelled")
        except Exception as error:
            return RunResult(
                RunOutcome.FAILED,
                "typed operation failed",
                metadata={"error_type": type(error).__name__},
            )

    async def _execute(self, run_id: str, execution: ExecutionContract) -> RunResult:
        operation = execution.operation
        if isinstance(operation, BuildImageOperation):
            source = operation.source_input
            if not isinstance(source, ArtifactRef):
                raise OperationError("build operation input must be resolved")
            if (
                self._repository_allowlist is not None
                and operation.source_repository not in self._repository_allowlist
            ):
                raise OperationError("source repository is not allowlisted")
            repositories = self._repository_allowlist or frozenset(
                {operation.source_repository}
            )
            key = f"build:{operation.source_repository}:{source.value}"
            async with self._operation_lock(key):
                cache = GitRepositoryCache(
                    self._builder.cache_root / "git",
                    repositories,
                    self._builder.runner,
                )
                workspace = await cache.checkout(operation.source_repository, source)
                try:
                    artifact = await self._builder.build(operation, workspace)
                finally:
                    await cache.cleanup(workspace)
            return RunResult(
                RunOutcome.COMPLETED, "image built", produced_artifacts=(artifact,)
            )
        if isinstance(operation, DeployImageOperation):
            if not isinstance(operation.image_input, ArtifactRef):
                raise OperationError("deploy operation input must be resolved")
            key = f"deploy:{operation.target}:{operation.deployment_name}"
            async with self._operation_lock(key):
                await self._deployer.deploy(operation)
            return RunResult(RunOutcome.COMPLETED, "image deployed")
        raise OperationError(f"unsupported typed operation for run {run_id}")

    @asynccontextmanager
    async def _operation_lock(self, key: str) -> AsyncIterator[None]:
        state = self._operation_locks.get(key)
        if state is None:
            state = _OperationLockState(asyncio.Lock())
            self._operation_locks[key] = state
        state.users += 1
        try:
            async with state.lock:
                yield
        finally:
            state.users -= 1
            if state.users == 0 and self._operation_locks.get(key) is state:
                self._operation_locks.pop(key, None)


__all__ = [
    "CommandResult",
    "CommandRunner",
    "DeploymentTarget",
    "DockerComposeDeployer",
    "DockerImageBuilder",
    "GitRepositoryCache",
    "GitWorkspace",
    "OperationError",
    "OperationHarnessDriver",
    "SubprocessCommandRunner",
]
