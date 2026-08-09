#!/usr/bin/python3
"""Trusted payload executed inside direct and Codex-created sandboxes."""

from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

AUTH_FILE = Path("/home/bened/.local/share/agentd/codex-home/auth.json")
STATE_DATABASE = Path("/home/bened/.local/state/agentd/state.sqlite")
WORKSPACE_ROOT = Path("/home/bened/.local/share/agentd/workspaces")
REPOSITORY_ROOT = Path("/home/bened/goldenage")
UV_CACHE_ROOT = Path("/home/bened/.cache/uv")


def _is_readable(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _unexpectedly_writable(directory: Path) -> bool:
    probe = directory / f".agentd-write-denied-{os.getpid()}"
    try:
        descriptor = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError:
        return False
    try:
        os.close(descriptor)
        probe.unlink()
    finally:
        if probe.exists():
            probe.unlink()
    return True


def main() -> int:
    worktree = Path(os.environ.get("AGENTD_SECURITY_WORKTREE", ""))
    try:
        relative = worktree.relative_to(WORKSPACE_ROOT)
    except ValueError:
        print("security worktree is outside the exact workspace root", file=sys.stderr)
        return 39
    if len(relative.parts) != 1 or not relative.name.startswith(".security-lease."):
        print("security worktree is not a dedicated temporary lease", file=sys.stderr)
        return 39

    if _is_readable(AUTH_FILE):
        print("sandbox opened the dedicated auth.json", file=sys.stderr)
        return 40
    if _is_readable(STATE_DATABASE):
        print("sandbox opened the agentd state database", file=sys.stderr)
        return 41
    for directory in (REPOSITORY_ROOT, WORKSPACE_ROOT, UV_CACHE_ROOT):
        if _unexpectedly_writable(directory):
            print(
                f"sandbox wrote outside its leased worktree: {directory}",
                file=sys.stderr,
            )
            return 45

    write_probe = worktree / f".write-test-{os.getpid()}"
    try:
        descriptor = os.open(
            write_probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(descriptor, b"sandbox-write-ok\n")
        os.fsync(descriptor)
        os.close(descriptor)
        write_probe.unlink()
    except OSError as exc:
        print(f"sandbox could not write its leased worktree: {exc}", file=sys.stderr)
        return 43

    network_probe: socket.socket | None = None
    try:
        network_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # UDP connect selects a route but transmits no packet. TEST-NET-1 is
        # reserved for documentation and is never an agentd service endpoint.
        network_probe.connect(("192.0.2.1", 9))
    except OSError as exc:
        denied_errors = {
            errno.EACCES,
            errno.EHOSTUNREACH,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EPERM,
        }
        if exc.errno in denied_errors:
            return 0
        print(f"network probe failed unexpectedly: {exc}", file=sys.stderr)
        return 44
    finally:
        if network_probe is not None:
            network_probe.close()
    print("sandbox allowed an AF_INET network route", file=sys.stderr)
    return 42


if __name__ == "__main__":
    raise SystemExit(main())
