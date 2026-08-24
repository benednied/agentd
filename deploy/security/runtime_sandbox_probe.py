#!/usr/bin/python3
"""Fail-closed runtime proof for the nested agentd/Codex sandbox boundary."""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex import __version__ as installed_sdk_version
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    CommandExecResponse,
    PermissionProfileListResponse,
)

EXPECTED_SDK_VERSION = "0.144.4"
AUTH_FILE = Path("/home/bened/.local/share/agentd/codex-home/auth.json")
CONFIG_FILE = Path("/home/bened/.local/share/agentd/codex-home/config.toml")
STATE_DATABASE = Path("/home/bened/.local/state/agentd/state.sqlite")
WORKSPACE_ROOT = Path("/home/bened/.local/share/agentd/workspaces")
UV_CACHE_ROOT = Path("/home/bened/.cache/uv")
BWRAP = Path("/usr/bin/bwrap")
REAL_BWRAP = Path("/usr/libexec/agentd/bwrap.real")
BWRAP_AUDIT_DIRECTORY = Path("/run/agentd")
BWRAP_AUDIT_FILE = BWRAP_AUDIT_DIRECTORY / "bwrap-compat.audit"
BWRAP_AUDIT_TOKEN_ENV = "AGENTD_BWRAP_AUDIT_TOKEN"
PAYLOAD = Path("/opt/agentd/security/sandbox_payload.py")
EXPECTED_CONFIG = Path("/opt/agentd/security/config.toml")
PERMISSION_PROFILE = "agentd-workspace"
AGENTD_ENTRYPOINT = Path("/opt/agentd/venv/bin/agentd")
CODEX_HELPER_ALIASES = tuple(
    Path("/usr/libexec/agentd") / name
    for name in (
        "codex-linux-sandbox",
        "codex-execve-wrapper",
        "apply_patch",
        "applypatch",
    )
)


def _require_runtime_layout() -> None:
    for path, description in (
        (AUTH_FILE, "dedicated auth.json"),
        (CONFIG_FILE, "reviewed Codex config.toml"),
        (STATE_DATABASE, "agentd state database"),
        (WORKSPACE_ROOT, "workspace root"),
        (UV_CACHE_ROOT, "uv cache root"),
        (BWRAP, "bubblewrap"),
        (REAL_BWRAP, "private real bubblewrap"),
        (BWRAP_AUDIT_DIRECTORY, "bubblewrap audit directory"),
        (PAYLOAD, "sandbox probe payload"),
        (EXPECTED_CONFIG, "image-pinned Codex config.toml"),
        (AGENTD_ENTRYPOINT, "agentd service entrypoint"),
    ):
        if not path.exists():
            raise RuntimeError(f"missing {description}: {path}")
    for path in (AUTH_FILE, CONFIG_FILE, STATE_DATABASE):
        metadata = path.stat()
        if metadata.st_uid != 1000 or metadata.st_gid != 1000:
            raise RuntimeError(f"sensitive runtime file has unexpected owner: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError(f"sensitive runtime file must have mode 0600: {path}")
    for directory in (
        AUTH_FILE.parent,
        STATE_DATABASE.parent,
        WORKSPACE_ROOT,
        UV_CACHE_ROOT,
        BWRAP_AUDIT_DIRECTORY,
    ):
        metadata = directory.stat()
        if metadata.st_uid != 1000 or metadata.st_gid != 1000:
            raise RuntimeError(f"runtime directory has unexpected owner: {directory}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError(f"runtime directory must have mode 0700: {directory}")
    if CONFIG_FILE.read_bytes() != EXPECTED_CONFIG.read_bytes():
        raise RuntimeError(
            "dedicated Codex config.toml differs from the image-pinned policy"
        )
    for path in (BWRAP, REAL_BWRAP, PAYLOAD, EXPECTED_CONFIG, AGENTD_ENTRYPOINT):
        metadata = path.stat()
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise RuntimeError(f"image security artifact has unexpected owner: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise RuntimeError(
                f"image security artifact is group/world writable: {path}"
            )
    for executable in (BWRAP, REAL_BWRAP):
        if executable.read_bytes()[:4] != b"\x7fELF":
            raise RuntimeError(
                f"security executable is not an ELF binary: {executable}"
            )
        if not executable.stat().st_mode & stat.S_IXUSR:
            raise RuntimeError(f"security executable is not executable: {executable}")
    if BWRAP.samefile(REAL_BWRAP):
        raise RuntimeError("bubblewrap compatibility shim aliases the real binary")
    if AGENTD_ENTRYPOINT.read_bytes().splitlines()[0] not in {
        b"#!/opt/agentd/venv/bin/python",
        b"#!/opt/agentd/venv/bin/python3",
    }:
        raise RuntimeError(
            "agentd entrypoint does not use the final runtime interpreter"
        )
    runtime = bundled_codex_path()
    for alias in CODEX_HELPER_ALIASES:
        metadata = alias.lstat()
        if (
            not alias.is_symlink()
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or Path(os.readlink(alias)) != runtime
        ):
            raise RuntimeError(f"invalid pinned Codex helper alias: {alias}")
    resolved_bwrap = shutil.which("bwrap")
    if resolved_bwrap != str(BWRAP):
        raise RuntimeError(f"bwrap resolves to {resolved_bwrap!r}, expected {BWRAP}")
    if installed_sdk_version != EXPECTED_SDK_VERSION:
        raise RuntimeError(
            f"openai-codex {installed_sdk_version!r} does not match "
            f"the reviewed version {EXPECTED_SDK_VERSION!r}"
        )


def _probe_environment(worktree: Path) -> dict[str, str]:
    return {
        "AGENTD_SECURITY_WORKTREE": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _audit_environment(worktree: Path, token: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(_probe_environment(worktree))
    environment[BWRAP_AUDIT_TOKEN_ENV] = token
    return environment


def _reset_bwrap_audit() -> None:
    if not BWRAP_AUDIT_FILE.exists() and not BWRAP_AUDIT_FILE.is_symlink():
        return
    metadata = BWRAP_AUDIT_FILE.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 1000
        or metadata.st_gid != 1000
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("unsafe existing bubblewrap compatibility audit record")
    BWRAP_AUDIT_FILE.unlink()


def _require_bwrap_rewrite_audit(
    token: str,
    description: str,
    *,
    require_helper_rewrite: bool,
    allow_additional_device_rewrites: bool = False,
) -> None:
    if not BWRAP_AUDIT_FILE.is_file() or BWRAP_AUDIT_FILE.is_symlink():
        raise RuntimeError(f"{description} did not create a regular bwrap audit record")
    metadata = BWRAP_AUDIT_FILE.stat()
    if (
        metadata.st_uid != 1000
        or metadata.st_gid != 1000
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"{description} created an unsafe bwrap audit record")

    matching_rewrites = 0
    helper_rewrites = 0
    for line in BWRAP_AUDIT_FILE.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) != 6 or parts[0] != "v1":
            raise RuntimeError(f"{description} emitted a malformed bwrap audit record")
        try:
            fields = dict(part.split("=", 1) for part in parts[1:])
        except ValueError as error:
            raise RuntimeError(
                f"{description} emitted a malformed bwrap audit record"
            ) from error
        if fields.get("token") != token:
            raise RuntimeError(f"{description} emitted an unexpected bwrap audit token")
        if fields.get("rewrite_dev") == "1":
            matching_rewrites += 1
            if fields.get("unshare_net") != "1":
                raise RuntimeError(
                    f"{description} did not preserve network namespace isolation"
                )
        if fields.get("rewrite_helper") == "1":
            helper_rewrites += 1
            if fields.get("rewrite_dev") != "1" or fields.get("unshare_net") != "1":
                raise RuntimeError(
                    f"{description} rewrote a helper outside the isolated "
                    "device/network invocation"
                )
    if matching_rewrites < 1 or (
        matching_rewrites != 1 and not allow_additional_device_rewrites
    ):
        raise RuntimeError(
            f"{description} applied {matching_rewrites} audited /dev rewrites; "
            + (
                "expected at least 1"
                if allow_additional_device_rewrites
                else "expected 1"
            )
        )
    expected_helper_rewrites = 1 if require_helper_rewrite else 0
    if helper_rewrites != expected_helper_rewrites:
        raise RuntimeError(
            f"{description} applied {helper_rewrites} audited helper rewrites; "
            f"expected {expected_helper_rewrites}"
        )


def _run_shim_rejection_probes() -> None:
    for arguments in (
        ("--share-net", "--version"),
        ("--dev", "/tmp/dev", "--version"),
        ("--dev-bind", "/dev/null", "/dev/null", "--version"),
        ("--dev-bind-try", "/dev", "/dev", "--version"),
        ("--args", "0"),
        (
            "--",
            "/home/bened/.local/share/agentd/codex-home/tmp/arg0/"
            "codex-arg0AAAAAA/codex-linux-sandbox",
        ),
        (
            "/home/bened/.local/share/agentd/codex-home/tmp/arg0/"
            "codex-arg0AAAAAA/codex-linux-sandbox",
        ),
    ):
        result = subprocess.run(
            [str(BWRAP), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 64 or "compatibility shim:" not in result.stderr:
            raise RuntimeError(
                "bubblewrap compatibility shim accepted an unsafe invocation: "
                f"{arguments!r}"
            )


def _run_direct_bwrap_probe(worktree: Path) -> None:
    token = secrets.token_hex(16)
    _reset_bwrap_audit()
    result = subprocess.run(
        [
            str(BWRAP),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--ro-bind",
            "/proc",
            "/proc",
            "--tmpfs",
            str(AUTH_FILE.parent),
            "--remount-ro",
            str(AUTH_FILE.parent),
            "--tmpfs",
            str(STATE_DATABASE.parent),
            "--remount-ro",
            str(STATE_DATABASE.parent),
            "--bind",
            str(worktree),
            str(worktree),
            "--chdir",
            str(worktree),
            "--",
            sys.executable,
            str(PAYLOAD),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_audit_environment(worktree, token),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "direct nested bwrap security probe failed "
            f"({result.returncode}): {result.stderr.strip()}"
        )
    _require_bwrap_rewrite_audit(
        token,
        "direct nested sandbox probe",
        require_helper_rewrite=False,
    )


def _create_toolchain_launcher(token: str) -> Path:
    launcher = UV_CACHE_ROOT / f".agentd-security-python-{token}"
    try:
        with launcher.open("xb") as stream:
            stream.write(b'#!/bin/sh\nexec /opt/agentd/venv/bin/python "$@"\n')
            stream.flush()
            os.fsync(stream.fileno())
        launcher.chmod(0o500)
    except BaseException:
        launcher.unlink(missing_ok=True)
        raise
    return launcher


def _run_codex_generated_command_probe(worktree: Path) -> None:
    runtime = bundled_codex_path()
    if not runtime.is_file():
        raise RuntimeError(f"pinned Codex runtime is missing: {runtime}")
    runtime_metadata = runtime.stat()
    if runtime_metadata.st_uid != 0 or stat.S_IMODE(runtime_metadata.st_mode) & 0o022:
        raise RuntimeError(
            f"pinned Codex runtime is not root-owned/immutable: {runtime}"
        )
    token = secrets.token_hex(16)
    _reset_bwrap_audit()
    environment = _audit_environment(worktree, token)
    toolchain_launcher = _create_toolchain_launcher(token)
    try:
        workspace_payload = worktree / ".agentd-security-payload.py"
        shutil.copyfile(PAYLOAD, workspace_payload)
        workspace_payload.chmod(0o500)
        config = CodexConfig(
            cwd=str(worktree),
            env=environment,
            client_name="agentd_security_preflight",
            client_title="agentd security preflight",
            experimental_api=True,
        )
        payload = {
            "command": [str(toolchain_launcher), str(workspace_payload)],
            "cwd": str(worktree),
            "env": _probe_environment(worktree),
            "timeoutMs": 30_000,
            "outputBytesCap": 16_384,
            "permissionProfile": PERMISSION_PROFILE,
        }
        with CodexClient(config) as client:
            client.initialize()
            profiles = client.request(
                "permissionProfile/list",
                {"cwd": str(worktree)},
                response_model=PermissionProfileListResponse,
            )
            if not any(
                profile.id == PERMISSION_PROFILE and profile.allowed
                for profile in profiles.data
            ):
                raise RuntimeError(
                    f"required permission profile is unavailable: {PERMISSION_PROFILE}"
                )
            result = client.request(
                "command/exec",
                payload,
                response_model=CommandExecResponse,
            )
        if result.exit_code != 0:
            raise RuntimeError(
                "pinned Codex generated-command sandbox probe failed "
                f"({result.exit_code}): {result.stderr.strip()}"
            )
        _require_bwrap_rewrite_audit(
            token,
            "pinned Codex generated-command probe",
            require_helper_rewrite=True,
            allow_additional_device_rewrites=True,
        )
    finally:
        toolchain_launcher.unlink(missing_ok=True)


def main() -> int:
    _require_runtime_layout()
    worktree = Path(tempfile.mkdtemp(prefix=".security-lease.", dir=WORKSPACE_ROOT))
    try:
        _run_shim_rejection_probes()
        _run_direct_bwrap_probe(worktree)
        _run_codex_generated_command_probe(worktree)
    finally:
        _reset_bwrap_audit()
        shutil.rmtree(worktree)
    print(
        "audited bwrap compatibility and pinned Codex permission-profile "
        "invariants passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
