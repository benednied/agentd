"""Local administrative CLI for durable control-plane state."""

from __future__ import annotations

import argparse
import json
import platform
from collections.abc import Sequence
from pathlib import Path

from agentd.domain.enums import QoSClass
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
    parser = argparse.ArgumentParser(
        prog="agentd",
        description="Administer the local agent execution control plane",
    )
    parser.add_argument("--db", default=".agentd/state.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize the SQLite control-plane state")

    submit = commands.add_parser("submit", help="submit a job")
    submit.add_argument("--project", required=True)
    submit.add_argument("--repository", required=True)
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
    submit.add_argument("--quota-pool", default="default")
    submit.add_argument("--harness", action="append", default=[])
    submit.add_argument("--accept", action="append", default=[])
    submit.add_argument("--depends-on", action="append", default=[])

    show = commands.add_parser("job", help="inspect one job")
    show.add_argument("job_id")
    commands.add_parser("jobs", help="list jobs")

    history = commands.add_parser("history", help="show a job's state history")
    history.add_argument("job_id")

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
    show_quota = commands.add_parser("quota", help="inspect a quota pool")
    show_quota.add_argument("pool_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with _store(args.db) as store:
        from agentd.service import ControlPlane

        plane = ControlPlane(store)
        if args.command == "init":
            print(f"Initialized agentd state at {Path(args.db).expanduser()}")
        elif args.command == "submit":
            harnesses = tuple(args.harness or ["fake"])
            job = Job(
                project=args.project,
                repository=args.repository,
                objective=args.objective,
                dependencies=tuple(args.depends_on),
                priority=args.priority,
                qos=QoSClass(args.qos),
                preferred_harnesses=harnesses,
                allowed_harnesses=harnesses,
                effort=EffortEstimate(args.p50, args.p90, args.p99),
                quota_budget=QuotaBudget(
                    implementation=args.quota,
                    pool_id=args.quota_pool,
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
                        minimum_interactive_reserve=args.interactive_reserve,
                    )
                )
            )
        elif args.command == "quota":
            _print_model(plane.inspect_quota(args.pool_id))
        else:  # pragma: no cover - argparse enforces known subcommands
            raise AssertionError(f"Unhandled command {args.command}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
