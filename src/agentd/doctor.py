"""Read-only service preflight checks used by ``agentd doctor``."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from agentd.config import ServiceConfig

PINNED_OPENAI_CODEX_VERSION = "0.144.4"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.ok for check in self.checks)


def run_doctor(config: ServiceConfig) -> DoctorReport:
    """Inspect prerequisites without changing local or provider state."""

    checks: list[DoctorCheck] = [
        DoctorCheck(
            "python",
            sys.version_info >= (3, 12),
            sys.version.split()[0],
        )
    ]
    for executable in ("git", "uv", "bwrap"):
        resolved = shutil.which(executable)
        checks.append(
            DoctorCheck(
                executable,
                resolved is not None,
                resolved or "not found on PATH",
            )
        )

    try:
        sdk_version = version("openai-codex")
    except PackageNotFoundError:
        sdk_version = "not installed"
    checks.append(
        DoctorCheck(
            "openai-codex",
            sdk_version == PINNED_OPENAI_CODEX_VERSION,
            sdk_version,
        )
    )
    checks.extend(_path_checks(config))
    checks.append(_sqlite_check(config.database))
    checks.append(_user_namespace_check())
    return DoctorReport(tuple(checks))


def _path_checks(config: ServiceConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for name, path in (
        ("state-directory", config.database.parent),
        ("workspace-root", config.workspace_root),
        ("codex-home", config.codex_home),
    ):
        exists = path.is_dir()
        writable = exists and os.access(path, os.W_OK | os.X_OK)
        checks.append(
            DoctorCheck(
                name,
                writable,
                str(path) if exists else f"missing: {path}",
            )
        )

    if config.codex_home.is_dir():
        mode = stat.S_IMODE(config.codex_home.stat().st_mode)
        checks.append(DoctorCheck("codex-home-mode", mode == 0o700, oct(mode)))
    else:
        checks.append(DoctorCheck("codex-home-mode", False, "directory missing"))
    auth = config.codex_home / "auth.json"
    if auth.is_file() and not auth.is_symlink():
        mode = stat.S_IMODE(auth.stat().st_mode)
        checks.append(DoctorCheck("codex-auth-mode", mode == 0o600, oct(mode)))
    else:
        checks.append(DoctorCheck("codex-auth-mode", False, "auth.json missing"))
    return checks


def _sqlite_check(database: Path) -> DoctorCheck:
    if not database.exists():
        return DoctorCheck("sqlite", True, "database will be initialized")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        return DoctorCheck("sqlite", False, str(error))
    detail = str(result[0]) if result else "no result"
    return DoctorCheck("sqlite", detail == "ok", detail)


def _user_namespace_check() -> DoctorCheck:
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        return DoctorCheck("nested-user-namespace", False, "bwrap not found")
    try:
        result = subprocess.run(
            (
                bubblewrap,
                "--ro-bind",
                "/",
                "/",
                "--unshare-user",
                "--uid",
                "0",
                "--gid",
                "0",
                "--",
                "/bin/true",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return DoctorCheck("nested-user-namespace", False, str(error))
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return DoctorCheck(
        "nested-user-namespace",
        result.returncode == 0,
        detail or f"exit {result.returncode}",
    )
