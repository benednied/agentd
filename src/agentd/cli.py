"""Local administrative CLI for durable control-plane state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import signal
import sys
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
from agentd.state.sqlite import SQLiteStateStore


def _print_model(model: Serializable) -> None:
    print(json.dumps(model.to_dict(), indent=2, sort_keys=True))


def _store(path: str) -> SQLiteStateStore:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(resolved)


def build_parser() -> argparse.ArgumentParser:
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        values = dict(os.environ)
        values.update(
            AGENTD_DB=args.db,
            AGENTD_WORKSPACE_ROOT=args.workspace_root,
            AGENTD_CODEX_HOME=args.codex_home,
        )
        return asyncio.run(_serve_service(ServiceConfig.from_environment(values)))
    if args.command == "doctor":
        from agentd.doctor import run_doctor

        values = dict(os.environ)
        values.update(
            AGENTD_DB=args.db,
            AGENTD_WORKSPACE_ROOT=args.workspace_root,
            AGENTD_CODEX_HOME=args.codex_home,
        )
        report = run_doctor(ServiceConfig.from_environment(values))
        print(json.dumps([asdict(check) for check in report.checks], indent=2))
        return 0 if report.healthy else 1
    if args.command == "accept":
        from agentd.coordinator import SchedulerCoordinator
        from agentd.harness.registry import DriverRegistry
        from agentd.service import ControlPlane
        from agentd.workspaces.git import GitWorkspaceManager

        with _store(args.db) as store:
            coordinator = SchedulerCoordinator(
                store,
                GitWorkspaceManager(args.workspace_root),
                DriverRegistry(),
            )
            accepted = asyncio.run(
                ControlPlane(store, coordinator=coordinator).accept(args.job_id)
            )
            _print_model(accepted)
        return 0
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
    if args.command == "repair":
        from agentd.service import ControlPlane

        with _store(args.db) as store:
            _print_model(
                ControlPlane(store).request_repair(args.job_id, args.instruction)
            )
        return 0
    with _store(args.db) as store:
        from agentd.service import ControlPlane

        plane = ControlPlane(store)
        if args.command == "init":
            print(f"Initialized agentd state at {Path(args.db).expanduser()}")
        elif args.command == "submit":
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
                    unit=(
                        QuotaUnit.TOKENS if "codex" in harnesses else QuotaUnit.ABSTRACT
                    ),
                ),
                acceptance_criteria=tuple(args.accept),
            )
            _print_model(plane.submit(job))
        elif args.command == "job":
            _print_model(plane.inspect_job(args.job_id))
        elif args.command == "jobs":
            print(
                json.dumps(
                    [job.to_dict() for job in plane.list_jobs()],
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "history":
            print(
                json.dumps(
                    [event.to_dict() for event in plane.history(args.job_id)],
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "usage":
            run_ids = (
                [args.usage_run_id]
                if args.usage_run_id is not None
                else [run.id for run in store.list_runs(args.usage_job_id)]
            )
            print(
                json.dumps(
                    {
                        run_id: [
                            sample.to_dict()
                            for sample in store.list_usage_samples(run_id)
                        ]
                        for run_id in run_ids
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "register-node":
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
        elif args.command == "nodes":
            print(
                json.dumps(
                    [node.to_dict() for node in plane.list_nodes()],
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "register-quota":
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
        elif args.command == "quota":
            _print_model(plane.inspect_quota(args.pool_id))
        else:  # pragma: no cover - argparse enforces known subcommands
            raise AssertionError(f"Unhandled command {args.command}")
    return 0


async def _serve_service(config: ServiceConfig) -> int:
    from agentd.bootstrap import create_local_runtime
    from agentd.daemon import AgentDaemon

    runtime = create_local_runtime(
        config.database,
        config.workspace_root,
        config=config,
        trusted_provisioning=True,
        enforce_codex_account_policy=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)

    def report(error: Exception) -> None:
        print(f"agentd: {error}", file=sys.stderr, flush=True)

    daemon = AgentDaemon(
        runtime.control_plane,
        poll_interval=config.poll_interval_seconds,
        account_oracle=runtime.account_oracle,
        account_poll_seconds=config.account_poll_seconds,
        on_error=report,
    )
    try:
        await daemon.serve(stop)
    finally:
        await runtime.aclose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
