"""Local administrative CLI for durable control-plane state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import signal
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from agentd.config import ServiceConfig
from agentd.domain.enums import QoSClass, QuotaUnit
from agentd.domain.models import (
    EffortEstimate,
    Job,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    Serializable,
    WorkerNode,
)
from agentd.observability import configure_logging, event_logger
from agentd.service import ControlPlane
from agentd.state.sqlite import SQLiteStateStore


def _print_model(model: Serializable) -> None:
    print(json.dumps(model.to_dict(), indent=2, sort_keys=True))


def _store(path: str) -> SQLiteStateStore:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(resolved)


def build_parser() -> argparse.ArgumentParser:
    """Build the stable administrative command-line interface."""

    defaults = ServiceConfig.from_environment(os.environ)
    parser = argparse.ArgumentParser(
        prog="agentd",
        description="Administer the local agent execution control plane",
    )
    parser.add_argument("--db", default=str(defaults.database))
    parser.add_argument("--workspace-root", default=str(defaults.workspace_root))
    parser.add_argument("--codex-home", default=str(defaults.codex_home))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize the SQLite control-plane state")
    commands.add_parser("serve", help="run the continuous local scheduler service")
    commands.add_parser("doctor", help="run read-only service prerequisite checks")

    worker = commands.add_parser(
        "worker-serve",
        help="run one authenticated remote worker daemon",
    )
    worker.add_argument("--host")
    worker.add_argument("--port", type=int)
    worker.add_argument("--node-id")
    worker.add_argument("--session-epoch")
    worker.add_argument("--journal")
    worker.add_argument("--psk-file")
    worker.add_argument("--tls-cert")
    worker.add_argument("--tls-key")
    worker.add_argument("--operations-config")
    worker.add_argument("--cache-root")
    worker.add_argument("--operation-state-root")
    worker.add_argument("--allow-insecure-loopback", action="store_true")

    submit = commands.add_parser("submit", help="submit a job")
    submit.add_argument("--project", required=True)
    submit.add_argument("--repository", required=True)
    submit.add_argument(
        "--base-ref",
        default="HEAD",
        help="exact Git ref or commit used to create the isolated worktree",
    )
    submit.add_argument("--objective", required=True)
    submit.add_argument(
        "--qos",
        choices=[item.value for item in QoSClass],
        default="normal",
    )
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--p50", type=float, required=True)
    submit.add_argument("--p90", type=float, required=True)
    submit.add_argument("--p99", type=float)
    submit.add_argument("--quota", type=float, required=True)
    submit.add_argument("--quota-maximum", type=float)
    submit.add_argument("--quota-pool", default="default")
    submit.add_argument("--harness", action="append", default=[])
    submit.add_argument("--model-class")
    submit.add_argument("--accept", action="append", default=[])
    submit.add_argument("--depends-on", action="append", default=[])

    show = commands.add_parser("job", help="inspect one job")
    show.add_argument("job_id")
    commands.add_parser("jobs", help="list jobs")

    history = commands.add_parser("history", help="show a job's state history")
    history.add_argument("job_id")

    usage = commands.add_parser("usage", help="show the append-only usage ledger")
    usage_target = usage.add_mutually_exclusive_group(required=True)
    usage_target.add_argument("--job", dest="usage_job_id")
    usage_target.add_argument("--run", dest="usage_run_id")

    accept = commands.add_parser("accept", help="accept a job currently in REVIEW")
    accept.add_argument("job_id")

    review = commands.add_parser(
        "review",
        help="promote a completed suspended checkpoint after operator validation",
    )
    review.add_argument("job_id")

    repair = commands.add_parser(
        "repair",
        help="request one bounded same-thread repair for a job in REVIEW",
    )
    repair.add_argument("job_id")
    repair.add_argument("--instruction", required=True)

    codex_status = commands.add_parser(
        "codex-status",
        help="refresh and show authoritative ChatGPT Codex quota status",
    )
    codex_status.add_argument("--pool", default="codex")
    codex_status.add_argument(
        "--bucket",
        help="show one bucket instead of the most restrictive reported bucket",
    )

    node = commands.add_parser("register-node", help="register or update a node")
    node.add_argument("node_id")
    node.add_argument("--os", default=platform.system().lower())
    node.add_argument("--arch", default=platform.machine().lower())
    node.add_argument("--cpu", type=float, required=True)
    node.add_argument("--ram-gb", type=float, required=True)
    node.add_argument("--gpu-count", type=int, default=0)
    node.add_argument("--vram-gb", type=float, default=0)
    node.add_argument("--harness", action="append", required=True)
    node.add_argument("--capability", action="append", default=[])
    commands.add_parser("nodes", help="list worker nodes")

    quota = commands.add_parser(
        "register-quota", help="register or update a quota pool"
    )
    quota.add_argument("pool_id")
    quota.add_argument("--provider", required=True)
    quota.add_argument("--remaining", type=float, required=True)
    quota.add_argument("--interactive-reserve", type=float, default=0)
    quota.add_argument(
        "--unit",
        choices=[item.value for item in QuotaUnit],
        default=QuotaUnit.ABSTRACT.value,
    )
    show_quota = commands.add_parser("quota", help="inspect a quota pool")
    show_quota.add_argument("pool_id")
    return parser


def _service_config(args: argparse.Namespace) -> ServiceConfig:
    values = dict(os.environ)
    values.update(
        AGENTD_DB=args.db,
        AGENTD_WORKSPACE_ROOT=args.workspace_root,
        AGENTD_CODEX_HOME=args.codex_home,
    )
    return ServiceConfig.from_environment(values)


def _lifecycle_plane(
    args: argparse.Namespace,
    store: SQLiteStateStore,
) -> ControlPlane:
    from agentd.coordinator import SchedulerCoordinator
    from agentd.harness.registry import DriverRegistry
    from agentd.workspaces.git import GitWorkspaceManager

    coordinator = SchedulerCoordinator(
        store,
        GitWorkspaceManager(args.workspace_root),
        DriverRegistry(),
    )
    return ControlPlane(store, coordinator=coordinator)


def _run_process_command(
    args: argparse.Namespace,
    config: ServiceConfig,
) -> int | None:
    if args.command == "serve":
        return asyncio.run(_serve_service(config))
    if args.command == "worker-serve":
        return _serve_worker(args)
    if args.command == "doctor":
        from agentd.doctor import run_doctor

        report = run_doctor(config)
        print(json.dumps([asdict(check) for check in report.checks], indent=2))
        return 0 if report.healthy else 1
    if args.command == "codex-status":
        from agentd.runtime.codex_oracle import CodexAccountOracle

        with _store(args.db) as store:
            snapshot = asyncio.run(
                CodexAccountOracle(
                    args.pool,
                    bucket_id=args.bucket,
                    store=store,
                ).snapshot()
            )
            _print_model(snapshot)
        return 0
    return None


def _serve_worker(args: argparse.Namespace) -> int:
    from agentd.workers.bootstrap import WorkerServeConfig, run_worker_server

    values = dict(os.environ)
    overrides = {
        "AGENTD_WORKER_HOST": args.host,
        "AGENTD_WORKER_PORT": str(args.port) if args.port is not None else None,
        "AGENTD_WORKER_NODE_ID": args.node_id,
        "AGENTD_WORKER_SESSION_EPOCH": args.session_epoch,
        "AGENTD_WORKER_JOURNAL": args.journal,
        "AGENTD_WORKER_PSK_FILE": args.psk_file,
        "AGENTD_WORKER_TLS_CERT": args.tls_cert,
        "AGENTD_WORKER_TLS_KEY": args.tls_key,
        "AGENTD_WORKER_OPERATIONS_CONFIG": args.operations_config,
        "AGENTD_WORKER_CACHE_ROOT": args.cache_root,
        "AGENTD_WORKER_OPERATION_STATE_ROOT": args.operation_state_root,
    }
    for name, value in overrides.items():
        if value is not None:
            values[name] = value
    if args.allow_insecure_loopback:
        values["AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK"] = "1"
    config = WorkerServeConfig.from_environment(values)
    event_logger(component="worker").info("worker_starting")
    result = asyncio.run(run_worker_server(config, values=values))
    event_logger(component="worker").info("worker_stopped")
    return result


def _run_lifecycle_command(args: argparse.Namespace) -> int | None:
    if args.command not in {"accept", "review", "repair"}:
        return None
    with _store(args.db) as store:
        if args.command == "repair":
            result = ControlPlane(store).request_repair(
                args.job_id,
                args.instruction,
            )
        else:
            plane = _lifecycle_plane(args, store)
            if args.command == "accept":
                result = asyncio.run(plane.accept(args.job_id))
            else:
                result = plane.promote_suspended_to_review(args.job_id)
        _print_model(result)
    return 0


def _submit(
    args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    harnesses = tuple(args.harness or ["fake"])
    model_class = args.model_class or (
        "gpt-5.6-terra" if "codex" in harnesses else "standard"
    )
    job = Job(
        project=args.project,
        repository=args.repository,
        objective=args.objective,
        base_ref=args.base_ref,
        dependencies=tuple(args.depends_on),
        priority=args.priority,
        qos=QoSClass(args.qos),
        preferred_harnesses=harnesses,
        allowed_harnesses=harnesses,
        preferred_model_class=model_class,
        minimum_model_class=model_class,
        effort=EffortEstimate(args.p50, args.p90, args.p99),
        quota_budget=QuotaBudget(
            implementation=args.quota,
            maximum=args.quota_maximum,
            pool_id=args.quota_pool,
            unit=(QuotaUnit.TOKENS if "codex" in harnesses else QuotaUnit.ABSTRACT),
        ),
        acceptance_criteria=tuple(args.accept),
    )
    _print_model(plane.submit(job))


def _print_jobs(
    _args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    print(
        json.dumps(
            [job.to_dict() for job in plane.list_jobs()],
            indent=2,
            sort_keys=True,
        )
    )


def _print_history(
    args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    print(
        json.dumps(
            [event.to_dict() for event in plane.history(args.job_id)],
            indent=2,
            sort_keys=True,
        )
    )


def _print_usage(
    args: argparse.Namespace,
    _plane: ControlPlane,
    store: SQLiteStateStore,
) -> None:
    run_ids = (
        [args.usage_run_id]
        if args.usage_run_id is not None
        else [run.id for run in store.list_runs(args.usage_job_id)]
    )
    print(
        json.dumps(
            {
                run_id: [
                    sample.to_dict() for sample in store.list_usage_samples(run_id)
                ]
                for run_id in run_ids
            },
            indent=2,
            sort_keys=True,
        )
    )


def _register_node(
    args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    _print_model(
        plane.register_node(
            WorkerNode(
                id=args.node_id,
                labels={"os": args.os, "arch": args.arch},
                capacity=ResourceVector(
                    cpu=args.cpu,
                    ram_gb=args.ram_gb,
                    gpu_count=args.gpu_count,
                    vram_gb=args.vram_gb,
                ),
                harnesses=frozenset(args.harness),
                capabilities=frozenset(args.capability),
            )
        )
    )


def _print_nodes(
    _args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    print(
        json.dumps(
            [node.to_dict() for node in plane.list_nodes()],
            indent=2,
            sort_keys=True,
        )
    )


def _register_quota(
    args: argparse.Namespace,
    plane: ControlPlane,
    _store: SQLiteStateStore,
) -> None:
    _print_model(
        plane.register_quota_pool(
            QuotaPool(
                id=args.pool_id,
                provider=args.provider,
                remaining=args.remaining,
                unit=QuotaUnit(args.unit),
                minimum_interactive_reserve=args.interactive_reserve,
            )
        )
    )


def _run_store_command(args: argparse.Namespace) -> int:
    with _store(args.db) as store:
        plane = ControlPlane(store)
        if args.command == "init":
            print(f"Initialized agentd state at {Path(args.db).expanduser()}")
            return 0
        handlers = {
            "submit": _submit,
            "job": lambda item, api, state: _print_model(api.inspect_job(item.job_id)),
            "jobs": _print_jobs,
            "history": _print_history,
            "usage": _print_usage,
            "register-node": _register_node,
            "nodes": _print_nodes,
            "register-quota": _register_quota,
            "quota": lambda item, api, state: _print_model(
                api.inspect_quota(item.pool_id)
            ),
        }
        try:
            handler = handlers[args.command]
        except KeyError as error:  # pragma: no cover - argparse owns validation
            raise AssertionError(f"Unhandled command {args.command}") from error
        handler(args, plane, store)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse one administrative command and delegate it to a focused handler."""

    args = build_parser().parse_args(argv)
    config = _service_config(args)
    configure_logging(level=config.log_level, json_output=config.log_json)
    result = _run_process_command(args, config)
    if result is not None:
        return result
    result = _run_lifecycle_command(args)
    return result if result is not None else _run_store_command(args)


async def _serve_service(config: ServiceConfig) -> int:
    from agentd.bootstrap import create_local_runtime
    from agentd.daemon import AgentDaemon
    from agentd.workers.controller import RemoteWorkerController

    remote_workers = RemoteWorkerController.from_environment(os.environ)
    for endpoint in remote_workers.endpoints:
        if endpoint.psk_env is not None:
            # Clients retain the key in memory. Child processes started for
            # local Git/Codex work must never inherit a worker transport PSK.
            os.environ.pop(endpoint.psk_env, None)
    try:
        runtime = create_local_runtime(
            config.database,
            config.workspace_root,
            config=config,
            trusted_provisioning=True,
            enforce_codex_account_policy=True,
            additional_drivers=remote_workers.drivers,
            worker_backends=remote_workers.backends,
        )
    except BaseException:
        await remote_workers.close()
        raise
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)

    daemon = AgentDaemon(
        runtime.control_plane,
        poll_interval=config.poll_interval_seconds,
        account_oracle=runtime.account_oracle,
        account_poll_seconds=config.account_poll_seconds,
        worker_heartbeat_seconds=config.worker_heartbeat_seconds,
        provider_reset_remaining=config.provider_reset_remaining,
    )
    event_logger(component="daemon").info("service_started")
    try:
        await daemon.serve(stop)
    finally:
        await runtime.aclose()
        event_logger(component="daemon").info("service_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
