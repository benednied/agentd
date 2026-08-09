#!/usr/bin/python3
"""Fail-closed runtime proof for the nested agentd/Codex sandbox boundary."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex import __version__ as installed_sdk_version
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import CommandExecParams, CommandExecResponse

EXPECTED_SDK_VERSION = "0.144.4"
AUTH_FILE = Path("/home/bened/.local/share/agentd/codex-home/auth.json")
CONFIG_FILE = Path("/home/bened/.local/share/agentd/codex-home/config.toml")
STATE_DATABASE = Path("/home/bened/.local/state/agentd/state.sqlite")
WORKSPACE_ROOT = Path("/home/bened/.local/share/agentd/workspaces")
WRAPPER = Path("/usr/local/libexec/agentd/bwrap")
PAYLOAD = Path("/opt/agentd/security/sandbox_payload.py")
EXPECTED_CONFIG = Path("/opt/agentd/security/config.toml")
AUDIT_NAME = ".agentd-bwrap-invoked"
AUDIT_VALUE = "agentd-bwrap-wrapper-v1\n"


def _require_runtime_layout() -> None:
    for path, description in (
        (AUTH_FILE, "dedicated auth.json"),
        (CONFIG_FILE, "reviewed Codex config.toml"),
        (STATE_DATABASE, "agentd state database"),
        (WORKSPACE_ROOT, "workspace root"),
        (WRAPPER, "agentd bwrap wrapper"),
        (PAYLOAD, "sandbox probe payload"),
        (EXPECTED_CONFIG, "image-pinned Codex config.toml"),
    ):
        if not path.exists():
            raise RuntimeError(f"missing {description}: {path}")
    for path in (AUTH_FILE, CONFIG_FILE, STATE_DATABASE):
        metadata = path.stat()
        if metadata.st_uid != 1000 or metadata.st_gid != 1000:
            raise RuntimeError(f"sensitive runtime file has unexpected owner: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError(f"sensitive runtime file must have mode 0600: {path}")
    for directory in (AUTH_FILE.parent, STATE_DATABASE.parent, WORKSPACE_ROOT):
        metadata = directory.stat()
        if metadata.st_uid != 1000 or metadata.st_gid != 1000:
            raise RuntimeError(f"runtime directory has unexpected owner: {directory}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError(f"runtime directory must have mode 0700: {directory}")
    if CONFIG_FILE.read_bytes() != EXPECTED_CONFIG.read_bytes():
        raise RuntimeError(
            "dedicated Codex config.toml differs from the image-pinned policy"
        )
    for path in (WRAPPER, PAYLOAD, EXPECTED_CONFIG, Path("/usr/bin/bwrap")):
        metadata = path.stat()
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise RuntimeError(f"image security artifact has unexpected owner: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise RuntimeError(
                f"image security artifact is group/world writable: {path}"
            )
    resolved_bwrap = shutil.which("bwrap")
    if resolved_bwrap != str(WRAPPER):
        raise RuntimeError(
            f"bwrap resolves to {resolved_bwrap!r}, expected hardened wrapper {WRAPPER}"
        )
    if installed_sdk_version != EXPECTED_SDK_VERSION:
        raise RuntimeError(
            f"openai-codex {installed_sdk_version!r} does not match "
            f"the reviewed version {EXPECTED_SDK_VERSION!r}"
        )


def _probe_environment(worktree: Path, audit_file: Path) -> dict[str, str]:
    return {
        "AGENTD_SECURITY_WORKTREE": str(worktree),
        "AGENTD_BWRAP_AUDIT_FILE": str(audit_file),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _prepare_audit_file(audit_file: Path) -> None:
    audit_file.unlink(missing_ok=True)


def _require_wrapper_audit(audit_file: Path, phase: str) -> None:
    try:
        contents = audit_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"{phase} bypassed the agentd bwrap wrapper") from exc
    if contents != AUDIT_VALUE:
        raise RuntimeError(f"{phase} produced an invalid bwrap wrapper audit marker")


def _run_direct_bwrap_probe(worktree: Path, audit_file: Path) -> None:
    _prepare_audit_file(audit_file)
    environment = os.environ.copy()
    environment.update(_probe_environment(worktree, audit_file))
    result = subprocess.run(
        [
            str(WRAPPER),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
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
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "direct nested bwrap security probe failed "
            f"({result.returncode}): {result.stderr.strip()}"
        )
    _require_wrapper_audit(audit_file, "direct nested bwrap")


def _run_codex_generated_command_probe(worktree: Path, audit_file: Path) -> None:
    _prepare_audit_file(audit_file)
    runtime = bundled_codex_path()
    if not runtime.is_file():
        raise RuntimeError(f"pinned Codex runtime is missing: {runtime}")
    runtime_metadata = runtime.stat()
    if runtime_metadata.st_uid != 0 or stat.S_IMODE(runtime_metadata.st_mode) & 0o022:
        raise RuntimeError(
            f"pinned Codex runtime is not root-owned/immutable: {runtime}"
        )
    environment = os.environ.copy()
    environment.update(_probe_environment(worktree, audit_file))
    config = CodexConfig(
        env=environment,
        client_name="agentd_security_preflight",
        client_title="agentd security preflight",
        experimental_api=True,
    )
    params = CommandExecParams.model_validate(
        {
            "command": [sys.executable, str(PAYLOAD)],
            "cwd": str(worktree),
            "env": _probe_environment(worktree, audit_file),
            "timeoutMs": 30_000,
            "outputBytesCap": 16_384,
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(worktree)],
                "networkAccess": False,
                "excludeSlashTmp": True,
                "excludeTmpdirEnvVar": True,
            },
        }
    )
    payload = params.model_dump(by_alias=True, exclude_none=True, mode="json")
    with CodexClient(config) as client:
        client.initialize()
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
    _require_wrapper_audit(audit_file, "pinned Codex generated command")


def main() -> int:
    _require_runtime_layout()
    worktree = Path(tempfile.mkdtemp(prefix=".security-lease.", dir=WORKSPACE_ROOT))
    audit_file = worktree / AUDIT_NAME
    try:
        _run_direct_bwrap_probe(worktree, audit_file)
        _run_codex_generated_command_probe(worktree, audit_file)
    finally:
        shutil.rmtree(worktree)
    print("nested bwrap and pinned Codex sandbox invariants passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
