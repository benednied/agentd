# agentd

`agentd` is a Python control plane for scheduling AI coding agents across harnesses
and execution environments. It sits above the harness: Codex or a fake driver
executes a bounded assignment, while `agentd` owns readiness, placement, quota,
workspace isolation, preemption, and lifecycle.

> Agents execute work. The control plane owns scheduling, resources, isolation,
> quota, preemption, and lifecycle.

This repository is an executable local MVP, not a distributed production service.
The implemented worker backend is a local process; SSH, container, Kubernetes,
Slurm, Claude, OpenClaw, and llama.cpp workers are extension points rather than
current adapters. The control plane itself has a reviewed container deployment.

## Implemented vertical slice

- Typed jobs with QoS, dependencies, effort distributions, accepted-artifact quota
  budgets, capabilities, and resource requirements.
- Explicit, validated job transitions with append-only SQLite transition history.
- Deterministic QoS/priority ordering, dependency and gang readiness, capability
  placement, and best-fit node selection.
- Atomic SQLite quota reservations and node resource allocations, including an
  interactive reserve and emergency-conservation admission.
- Bounded reconnaissance child jobs for hors-categorie work and explicit promotion
  after a structured result.
- Pure tail and reset/burn policies. Tail decisions remain an explicit Python API,
  while the daemon now applies live Codex account and cumulative-token policies.
- Exclusive Git branches/worktrees, lease validation, resume capsules, suspension,
  review, completion, and immutable commit fields for dependency handoffs.
- Fake, official Codex SDK/App Server, and legacy Codex CLI harness drivers behind
  capability protocols, plus a local worker backend.
- Durable Codex threads, streamed observations and token usage, restart recovery,
  provider-window snapshots, durable command acknowledgement, and explicit
  review/repair/acceptance handoffs.
- A Python control-plane facade, run-scoped model API, operational CLI, and
  continuously serving local daemon.

The detailed component, lifecycle, and durability boundaries are in
[docs/architecture.md](docs/architecture.md).

## Requirements and installation

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git, including `git worktree`
- A Codex-authenticated, dedicated `CODEX_HOME` when running real Codex work
- Bubblewrap and unprivileged user namespaces for the reviewed Linux service profile

The primary `codex` driver uses the official Python SDK and its pinned App Server
runtime; `uv sync --frozen` installs `openai-codex==0.144.4`, so it does not require
a separately installed `codex` executable on `PATH`. A PATH-resolved executable is
needed only for the optional legacy `codex-cli` driver. See the official
[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) and
[App Server](https://learn.chatgpt.com/docs/app-server) documentation for the
underlying interfaces.

From a checkout:

```bash
uv sync --frozen
uv run agentd --help
```

For development and verification:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The test suite uses temporary Git repositories, SQLite, fake nodes/quota, and fake
processes. It does not invoke an LLM or a real Codex process.

The reviewed single-host container, user-systemd, SHA deployment/rollback, and
security verification profile is documented in
[docs/deployment.md](docs/deployment.md). Its normal test path is static and never
contacts a deployment host or copies credentials.

## Local Python example

The repository passed to a job must be a local Git repository. This example uses
only `FakeHarnessDriver`, so dispatch does not invoke an LLM:

```python
import asyncio
from pathlib import Path

from agentd.agent_api import AgentAPI
from agentd.bootstrap import create_local_runtime
from agentd.domain.models import (
    EffortEstimate,
    Job,
    QuotaBudget,
    QuotaPool,
    ResourceVector,
    ResumeCapsule,
    WorkerNode,
)


async def main() -> None:
    repository = Path.cwd().resolve()
    with create_local_runtime(
        database=repository / ".agentd/state.sqlite",
        workspace_root=repository.parent / ".agentd-workspaces",
        include_codex_driver=False,
        include_codex_cli_driver=False,
    ) as runtime:
        plane = runtime.control_plane
        plane.register_node(
            WorkerNode(
                id="local",
                labels={"backend": "local"},
                capacity=ResourceVector(cpu=8, ram_gb=16),
                harnesses=frozenset({"fake"}),
            )
        )
        plane.register_quota_pool(
            QuotaPool(id="default", provider="fake", remaining=100)
        )
        job = plane.submit(
            Job(
                project="example",
                repository=str(repository),
                objective="Implement and validate the bounded change",
                effort=EffortEstimate(p50=10, p90=20, p99=40),
                quota_budget=QuotaBudget(
                    implementation=10,
                    review=2,
                    repair=3,
                    validation=1,
                ),
                acceptance_criteria=("Tests pass", "Changes are committed"),
            )
        )

        run = await plane.dispatch_next()
        assert run is not None

        worker = AgentAPI(plane)
        assignment = worker.get_assignment(run.id)
        await worker.checkpoint(
            run.id,
            ResumeCapsule(
                completed=("inspection",),
                next_steps=("implementation", "validation"),
            ),
        )
        result = await worker.complete(run.id)
        print(assignment.objective, result.state)


asyncio.run(main())
```

`ExecutionContract` is intentionally worker-facing: it includes the objective,
scope, acceptance criteria, dependency results, filesystem scope, completion
protocol, model class, and optional resume capsule. It does not include quota
balances, node IDs, resource scarcity, QoS ranks, or scheduler reasoning.

`AgentAPI` authenticates mutations with the opaque run ID and prevents an older run
attempt from controlling a newer one. Its six worker operations are
`get_assignment`, `request_refinement`, `report_blocker`, `checkpoint`,
`request_review`, and `complete`; `request_history` can inspect retained requests
for the same run. Refinement and blocker records are currently bounded in-memory
records; checkpoints and job transitions are durable.

## CLI and daemon

The CLI initializes and inspects SQLite state, submits jobs, registers capacity,
runs service preflights, starts the daemon, exposes the usage ledger and provider
status, and handles review decisions. A fake-only setup is:

```bash
uv run agentd --db .agentd/state.sqlite init
uv run agentd --db .agentd/state.sqlite register-quota default \
  --provider local-test --remaining 100 --interactive-reserve 20
uv run agentd --db .agentd/state.sqlite register-node local \
  --cpu 8 --ram-gb 16 --harness fake
uv run agentd --db .agentd/state.sqlite submit \
  --project example \
  --repository /absolute/path/to/a/git/repository \
  --objective "Implement the bounded change" \
  --p50 10 --p90 20 --p99 40 --quota 16 \
  --harness fake --accept "Tests pass"
uv run agentd --db .agentd/state.sqlite jobs
uv run agentd --db .agentd/state.sqlite job JOB_ID
uv run agentd --db .agentd/state.sqlite history JOB_ID
uv run agentd --db .agentd/state.sqlite usage --job JOB_ID
```

For real Codex work, register a token-denominated local pool and a compatible node,
and give every Codex job a cumulative maximum:

```bash
uv run agentd --db .agentd/state.sqlite register-quota codex \
  --provider openai-codex-chatgpt --remaining 500000 \
  --interactive-reserve 50000 --unit tokens
uv run agentd --db .agentd/state.sqlite register-node local \
  --cpu 8 --ram-gb 16 --harness codex
uv run agentd --db .agentd/state.sqlite submit \
  --project example \
  --repository /absolute/path/to/a/git/repository \
  --objective "Implement and validate the bounded change" \
  --p50 25000 --p90 75000 --p99 100000 \
  --quota 75000 --quota-maximum 100000 --quota-pool codex \
  --harness codex --model-class gpt-5.6-terra \
  --accept "Tests pass"
```

Run the preflight and continuous service with the same paths. `serve` enables the
trusted workspace provisioner and Codex account-admission policy, recovers durable
managed runs at startup, then polls account telemetry, reconciles live runs, and
dispatches ready work until it receives `SIGINT` or `SIGTERM`:

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  doctor
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  serve
```

Operational inspection and review commands include:

```bash
CODEX_HOME=/absolute/path/to/dedicated-codex-home \
  uv run agentd --db .agentd/state.sqlite \
  codex-status --pool codex --bucket codex
uv run agentd --db .agentd/state.sqlite usage --run RUN_ID
uv run agentd --db .agentd/state.sqlite repair JOB_ID \
  --instruction "Address the review findings and rerun validation"
uv run agentd --db .agentd/state.sqlite accept JOB_ID
```

The standalone `codex-status` command uses the process's `CODEX_HOME`; `serve`
passes its configured `--codex-home` to both the driver and account oracle.
`repair` durably queues a request for a job in `REVIEW`; the running daemon later
reacquires policy-compliant quota and node capacity and starts the repair. `accept`
finalizes an already persisted reviewed result. Pause, resume, cancellation,
checkpoint creation, reconnaissance promotion, and reset-event injection remain
Python `ControlPlane` operations rather than CLI subcommands.

## Fake and Codex drivers

`FakeHarnessDriver` is deterministic and in-process. It returns a configurable
`RunResult`, records calls, and is intended for scheduler/application tests and
safe local demonstrations.

`create_local_runtime()` registers three drivers by default:

- `fake`: deterministic and in-process, for tests and safe demonstrations;
- `codex`: the primary `CodexSdkDriver`, backed by the official Python SDK/App
  Server; and
- `codex-cli`: the legacy `CodexCliDriver`, backed by `codex exec --json`.

The SDK driver is fixed to `gpt-5.6-terra` with reasoning effort `medium`. It starts
or resumes a durable thread, streams turn and token-usage notifications, requires a
strict structured review result, and sends steer, checkpoint, suspend, interrupt,
and cancel commands through App Server. Only the leased worktree is writable;
network access is disabled and additional toolchain roots are requested read-only.
The worker receives the bounded `ExecutionContract`, never account percentages,
quota scarcity, node details, or policy thresholds.

The supervisor persists the App Server thread/turn IDs, runtime versions,
observation cursor, latest observation, usage samples, commands, and command
acknowledgements. After a service restart it cannot attach to the old stdio
transport or continue the exact interrupted turn. Instead, it opens a new App
Server process, resumes the same durable Codex thread in the validated worktree,
and starts a fresh recovery turn. The legacy `codex-cli` adapter remains
process-local and does not provide this recovery or live metering path. The old
`CodexDriver` Python import remains an alias for `CodexCliDriver` for compatibility.

### Usage, account telemetry, and review

App Server reports cumulative thread token totals. Agentd subtracts durable prior
turn baselines, stores per-turn cumulative samples, and atomically charges only
positive deltas. Exact event retries are free, and a distinct terminal marker can
repeat the last reading without charging again. A terminal turn without valid
matching usage enters `METERING_PENDING`; its reservation stays held and acceptance
is blocked until metering is reconciled.

Separately, `CodexAccountOracle` reads the App Server account rate-limit bucket and
persists primary/secondary percentages, window lengths and reset times, reached
state, plan type, and opaque credit fields. These provider percentages are policy
signals, not token balances: agentd never converts them into the local pool's
absolute `remaining` value. By default, fresh telemetry blocks background QoS at
75% used, admits only interactive/blocker work at 90%, and blocks all new work when
the limit or credits are exhausted. Stale telemetry admits only urgent work; a
known reset within 12 hours may enable explicitly eligible pre-reset burn work.

Successful SDK turns enter `REVIEW` after terminal usage is durable and scarce
capacity is released. An operator can accept the persisted result or request up to
two bounded repair turns. Each repair validates the retained worktree, reacquires
capacity, resumes the same durable thread, and returns to `REVIEW`; token usage and
the cumulative job maximum span the implementation and every repair turn.

## State ownership

The architecture treats the source repository as authoritative for issues/Beads,
plans, dependencies, acceptance criteria, review relationships, and accepted
decisions. The current MVP does not yet read or write Beads or compile repository
plans automatically: callers create `Job` snapshots and structured reconnaissance
results through the Python API.

SQLite owns control-plane execution records: job snapshots and transitions, nodes,
resource allocations, quota pools/reservations, workspaces, runs/contracts,
checkpoints, managed-driver sessions and observations, provider snapshots,
append-only usage samples, and durable run commands/acknowledgements. Git owns
source files, worker branches, commits, and uncommitted worker output. Agentd never
merges a worker branch into the integration branch.

## MVP limitations

- One local process, one SQLite database, one local-process worker backend, and no
  distributed leader election or transactional outbox.
- No HTTP/JSON service, MCP transport, web UI, SSH/container/cloud backend, or
  general remote worker transport.
- Startup recovery is specific to the managed SDK driver and resumes a durable
  thread with a new App Server process and turn; it cannot reattach the previous
  stdio transport. The legacy CLI adapter remains process-local.
- The pinned SDK's generated schema does not retain the newer restricted-read
  sandbox field. The reviewed Linux deployment therefore fails closed unless its
  outer Bubblewrap wrapper and runtime canary prove auth/state unreadable,
  worktree-only writes, and no model-command network route.
- Dependency and gang readiness are implemented, but gang launch is not atomic and
  there is no persisted barrier aggregate.
- Tail-governor decisions and explicit reset events still require a caller. Codex
  token metering and account-window polling are live, but provider percentages and
  opaque credits cannot be converted into an absolute token balance.
- `request_refinement` and `report_blocker` are not durable across process restart.
- SQLite creates its schema in place; there is no versioned migration system yet.
- There is no automated reviewer, merge, or integration policy. `REVIEW`, repair,
  and acceptance require an operator or external caller.
- Invalid terminal telemetry deliberately leaves a job in `METERING_PENDING`; the
  MVP has no automated provider-side reconciliation for that state.
- Commit handoff uses `RunResult.commit` or falls back to the workspace's current
  `HEAD`. That fallback does not prove the worker created a new commit or that an
  integrator accepted it.

## Package map

```text
src/agentd/
  domain/        runtime models and explicit job transitions
  scheduling/    pure readiness, priority, placement, tail, burn, reconnaissance
  state/         persistence protocol and SQLite implementation
  workspaces/    workspace protocol and Git worktree leases
  harness/       fake, SDK/App Server, legacy CLI drivers and run supervisor
  runtime/       quota/resources plus account and cumulative-usage policy
  workers/       execution-backend protocol and local backend
  coordinator.py effectful admission, compensation, and lifecycle sequencing
  service.py     transport-independent control-plane facade
  agent_api.py   run-scoped model-facing protocol
  daemon.py      recovery, telemetry, reconciliation and dispatch loop
```
