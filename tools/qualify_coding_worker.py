#!/usr/bin/env python3
"""Run an isolated, bounded typed coding worker in the reviewed container.

Example arguments are paths/identities supplied by the trusted operator. Tunnel
the loopback port over SSH, or configure TLS for a private container interface.
Secrets are read from a protected file and never passed to the coding harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import ssl
import stat
from contextlib import suppress
from pathlib import Path

from agentd.coding.models import RepositoryProfile
from agentd.lifecycle import ControllerLock
from agentd.state.sqlite import SQLiteStateStore
from agentd.workers.coding import CodingHarnessDriver
from agentd.workers.coding_runtime import create_verified_coding_sdk
from agentd.workers.journal import OperationJournal
from agentd.workers.server import WorkerServer


def parser(*, include_lifetime: bool = True) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    # Keep the production parser shape compatible with the persistent runner;
    # the omitted option must still be present as an explicit unbounded value.
    result.set_defaults(lifetime_seconds=None)
    result.add_argument("--profiles", type=Path, required=True)
    result.add_argument("--account-pool", required=True)
    result.add_argument("--state-root", type=Path, required=True)
    result.add_argument("--workspace-root", type=Path, required=True)
    result.add_argument("--secret-file", type=Path, required=True)
    result.add_argument("--ready-file", type=Path, required=True)
    result.add_argument("--node-id", required=True)
    result.add_argument("--session-epoch", required=True)
    result.add_argument("--model", default="gpt-5.6-luna")
    result.add_argument("--dependency-venv", type=Path)
    result.add_argument("--dependency-profile-id")
    result.add_argument("--dependency-python-root", type=Path)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--tls-cert", type=Path)
    result.add_argument("--tls-key", type=Path)
    result.add_argument("--port", type=int, default=0)
    if include_lifetime:
        result.add_argument(
            "--lifetime-seconds",
            type=float,
            default=None,
            help="test-only bound; omit for the persistent production worker",
        )
    result.add_argument(
        "--capture-run",
        action="append",
        default=[],
        help="Administrative capture of a retained terminal run; never starts a model",
    )
    return result


def load_secret(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("worker secret must be a regular mode-0600 file")
    if metadata.st_uid != os.getuid():
        raise ValueError("worker secret must belong to this user")
    secret = path.read_bytes()
    if len(secret) < 32 or len(secret) > 4096:
        raise ValueError("worker secret must contain between 32 and 4096 bytes")
    return secret


async def run(args: argparse.Namespace) -> None:
    # Retained SQLite claims are not a process lock. Fence both normal serving
    # and administrative capture against another owner of this worker journal.
    with ControllerLock(args.state_root.resolve() / "worker.sqlite"):
        await _run_locked(args)


async def _run_locked(args: argparse.Namespace) -> None:
    if args.lifetime_seconds is not None and not 0 < args.lifetime_seconds <= 86_400:
        raise ValueError("worker lifetime must be positive and at most one day")
    state_root = args.state_root.resolve()
    workspace_root = args.workspace_root.resolve()
    protected_state = Path("/home/bened/.local/state/agentd").resolve()
    protected_workspaces = Path("/home/bened/.local/share/agentd/workspaces").resolve()
    if not state_root.is_relative_to(protected_state):
        raise ValueError("state root must use the reviewed protected deployment tree")
    if not workspace_root.is_relative_to(protected_workspaces):
        raise ValueError("workspace root must use the reviewed deployment lease tree")
    if not args.secret_file.resolve().is_relative_to(protected_state):
        raise ValueError("worker secret must remain inside the protected state tree")
    # The pinned containment probe checks a protected state file at this exact
    # path. A dedicated worker volume has no controller DB there; initialize
    # an empty private probe DB once, never replace an existing file.
    probe_database = protected_state / "state.sqlite"
    try:
        descriptor = os.open(
            probe_database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        with SQLiteStateStore(probe_database):
            pass
    secret = load_secret(args.secret_file)
    raw_profiles = json.loads(args.profiles.read_text())
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("profiles file must contain a nonempty JSON list")
    profiles = [RepositoryProfile.from_dict(value) for value in raw_profiles]
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("profile identities must be unique")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    approved = {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR"}
    environment = {key: value for key, value in os.environ.items() if key in approved}
    sdk = await create_verified_coding_sdk(
        state_root / "sdk.sqlite",
        environment=environment,
        model=args.model,
        dependency_venv=args.dependency_venv,
        dependency_profile_id=args.dependency_profile_id,
        dependency_python_root=args.dependency_python_root,
    )
    coding = CodingHarnessDriver(
        workspace_root,
        {profile.id: profile for profile in profiles},
        {"codex": sdk},
        account_pools={"codex": args.account_pool},
    )
    if args.capture_run:
        try:
            for run_id in args.capture_run:
                checkpoint = await coding.capture_checkpoint(run_id)
                print(
                    json.dumps(
                        {
                            key: value
                            for key, value in checkpoint.items()
                            if key != "bundle_chunks"
                        }
                    ),
                    flush=True,
                )
        finally:
            await coding.close()
            await sdk.close()
        return
    journal = OperationJournal(
        state_root / "worker.sqlite",
        node_id=args.node_id,
        session_epoch=args.session_epoch,
    )
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("TLS certificate and key must be supplied together")
    context = None
    if args.tls_cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
    server = WorkerServer(
        args.host,
        args.port,
        node_id=args.node_id,
        session_epoch=args.session_epoch,
        secret=secret,
        drivers=(coding,),
        journal=journal,
        ssl_context=context,
        allow_insecure_loopback=context is None,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)
    try:
        host, port = await server.start()
        # Write only nonsecret readiness data after the sandbox proof and bind.
        ready = {
            "host": host,
            "port": port,
            "node_id": args.node_id,
            "session_epoch": args.session_epoch,
            "driver": coding.capabilities().name,
            "features": sorted(coding.capabilities().features),
        }
        CodingHarnessDriver._write(args.ready_file, ready)
        if args.lifetime_seconds is None:
            await stop.wait()
        else:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=args.lifetime_seconds)
    finally:
        await server.close()
        await coding.close()
        await sdk.close()


def main() -> None:
    asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    main()
