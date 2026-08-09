"""Trusted, credential-isolated repository dependency provisioning."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentd.domain.models import WorkspaceLease

CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str]],
    Awaitable[tuple[int, bytes, bytes]],
]


class RepositoryProvisioningError(RuntimeError):
    """Raised when the trusted dependency preparation step fails."""


@runtime_checkable
class RepositoryProvisioner(Protocol):
    """Prepare an isolated workspace before any model process is started."""

    async def prepare(self, lease: WorkspaceLease) -> None: ...


async def _run_command(
    arguments: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=cwd,
        env=dict(environment),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


@dataclass(frozen=True, slots=True)
class TrustedUvProvisioner:
    """Run locked ``uv sync`` before a model receives the workspace.

    Bubblewrap sees the container filesystem read-only, with only the leased
    worktree, uv cache, and an empty provisioning home writable. The service
    ``CODEX_HOME`` is replaced by an empty tmpfs for the duration, so package
    build hooks cannot read ChatGPT authentication even though provisioning is
    allowed network access.
    """

    codex_home: Path
    state_directory: Path
    cache_directory: Path
    provisioning_home: Path
    extras: tuple[str, ...] = ()
    uv_executable: str = "uv"
    bubblewrap_executable: str = "/usr/bin/bwrap"
    timeout_seconds: float = 900
    runner: CommandRunner = _run_command

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("Provisioning timeout must be positive")
        if any(not extra.strip() or extra.startswith("-") for extra in self.extras):
            raise ValueError("uv extras must be non-empty names")

    async def prepare(self, lease: WorkspaceLease) -> None:
        workspace = Path(lease.working_directory).resolve()
        codex_home = self.codex_home.expanduser().resolve()
        state_directory = self.state_directory.expanduser().resolve()
        cache = self.cache_directory.expanduser().resolve()
        provision_home = self.provisioning_home.expanduser().resolve()
        if not workspace.is_dir():
            raise RepositoryProvisioningError(
                f"Workspace does not exist for provisioning: {workspace}"
            )
        if workspace == codex_home or workspace in codex_home.parents:
            raise RepositoryProvisioningError(
                "The leased worktree cannot contain the service Codex home"
            )

        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        provision_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        uv_arguments = [self.uv_executable, "sync", "--frozen"]
        for extra in self.extras:
            uv_arguments.extend(("--extra", extra))
        arguments = (
            self.bubblewrap_executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(workspace),
            str(workspace),
            "--bind",
            str(cache),
            str(cache),
            "--bind",
            str(provision_home),
            str(provision_home),
            "--tmpfs",
            str(codex_home),
            "--tmpfs",
            str(state_directory),
            "--chdir",
            str(workspace),
            "--",
            *uv_arguments,
        )
        environment = self._environment(cache, provision_home)
        try:
            returncode, _stdout, stderr = await asyncio.wait_for(
                self.runner(arguments, workspace, environment),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as error:
            raise RepositoryProvisioningError(
                f"Dependency provisioning exceeded {self.timeout_seconds:g} seconds"
            ) from error
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RepositoryProvisioningError(
                f"Locked dependency provisioning exited with {returncode}: {detail}"
            )

    @staticmethod
    def _environment(cache: Path, provision_home: Path) -> dict[str, str]:
        allowed = {
            name: value
            for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if (value := os.environ.get(name)) is not None
        }
        return {
            **allowed,
            "HOME": str(provision_home),
            "UV_CACHE_DIR": str(cache),
            "UV_NO_CONFIG": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
