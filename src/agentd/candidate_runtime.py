"""Candidate-bound extension of trusted dependency materialization.

Only an administrator-populated uv cache is a package source. Preparation and
validation both remain networkless; cache misses are infrastructure failures.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from agentd.workers.dependency_prep import (
    DependencyPreparationError,
    DependencyRuntimePreparation,
    PreparationMount,
)

if TYPE_CHECKING:
    from agentd.publication import ValidationRunner


class CandidatePreparationError(RuntimeError):
    """A retained candidate needs an operator to repair its prerequisite."""


def lock_digest(checkout: Path) -> str:
    path = checkout / "uv.lock"
    if path.is_symlink() or not path.is_file():
        raise CandidatePreparationError("Candidate requires a regular uv.lock")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class CandidateRuntime:
    root: Path
    lock_sha256: str

    def verify(self, checkout: Path) -> None:
        if lock_digest(checkout) != self.lock_sha256:
            raise CandidatePreparationError("Candidate runtime/lock digest mismatch")
        if (self.root / "lock.sha256").read_text() != self.lock_sha256:
            raise CandidatePreparationError("Runtime receipt digest mismatch")


@dataclass(frozen=True)
class CandidateRuntimePreparation:
    cache: Path
    python: Path
    uv: Path
    runtime_roots: tuple[Path, ...]
    runner: ValidationRunner
    revision: str = "1"
    timeout: float = 900

    def __post_init__(self) -> None:
        if not all(path.is_absolute() for path in (self.cache, self.python, self.uv)):
            raise ValueError("Preparation paths must be absolute")
        if not self.revision or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Preparation requires a revision and positive timeout")

    @property
    def prerequisite(self) -> str:
        return hashlib.sha256(
            repr(
                (
                    self.cache,
                    self.python,
                    self.uv,
                    self.runtime_roots,
                    self.revision,
                )
            ).encode()
        ).hexdigest()

    def prepare(self, checkout: Path, destination: Path) -> CandidateRuntime:
        digest = lock_digest(checkout)
        # Copy metadata only. Never expose candidate source, hooks or credentials
        # to the dependency installer, and never install the project itself.
        staging = destination.parent / "dependency-input"
        staging.mkdir()
        for name in ("uv.lock", "pyproject.toml"):
            source = checkout / name
            if source.is_symlink() or not source.is_file():
                raise CandidatePreparationError("Dependency metadata must be regular")
            (staging / name).write_bytes(source.read_bytes())
        lock = tomllib.loads((staging / "uv.lock").read_text())
        packages = lock.get("package", [])
        if not isinstance(packages, list):
            raise CandidatePreparationError("Invalid lock packages")
        for package in packages:
            if not isinstance(package, dict):
                raise CandidatePreparationError("Invalid lock package")
            source = package.get("source", {})
            if not isinstance(source, dict):
                raise CandidatePreparationError("Invalid dependency source")
            if source not in ({"editable": "."}, {"virtual": "."}) and (
                set(source) != {"registry"}
                or source["registry"] != "https://pypi.org/simple"
            ):
                raise CandidatePreparationError("Unapproved dependency source")
            wheels = package.get("wheels", [])
            if not isinstance(wheels, list):
                raise CandidatePreparationError("Invalid wheel list")
            for wheel in wheels:
                if not isinstance(wheel, dict) or not all(
                    isinstance(wheel.get(key), str) for key in ("url", "hash")
                ):
                    raise CandidatePreparationError("Invalid wheel metadata")
                url = urlsplit(wheel["url"])
                if (
                    url.scheme != "https"
                    or url.netloc != "files.pythonhosted.org"
                    or url.fragment
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", wheel.get("hash", ""))
                ):
                    raise CandidatePreparationError("Unapproved wheel source or digest")
        shutil.copytree(self.cache, staging / "cache", symlinks=False)
        result = self.runner.run(
            (
                str(self.uv),
                "sync",
                "--frozen",
                "--offline",
                "--no-config",
                "--no-build",
                "--no-install-project",
                "--no-install-workspace",
                "--no-editable",
                "--no-managed-python",
                "--no-python-downloads",
                "--python",
                str(self.python),
                "--cache-dir",
                "/workspace/cache",
                "--link-mode",
                "copy",
            ),
            cwd=staging,
            env={"PATH": "/usr/bin:/bin"},
            timeout=self.timeout,
        )
        if result.returncode:
            raise CandidatePreparationError("Candidate dependency preparation failed")
        if lock_digest(staging) != digest or lock_digest(checkout) != digest:
            raise CandidatePreparationError("Candidate lock changed during preparation")
        destination.mkdir()
        try:
            DependencyRuntimePreparation(
                staging,
                (
                    PreparationMount(
                        staging / ".venv", "venv", Path("/workspace/.venv")
                    ),
                ),
                self.runtime_roots,
            ).prepare(destination)
        except DependencyPreparationError as error:
            raise CandidatePreparationError("Runtime materialization failed") from error
        (destination / "lock.sha256").write_text(digest)
        for path in (*destination.rglob("*"), destination):
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        artifact = CandidateRuntime(destination, digest)
        artifact.verify(checkout)
        return artifact
