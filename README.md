# agentd

**A local-first control plane for AI coding agents.**

`agentd` does not write code itself. It decides which job may run, reserves quota
and machine capacity, creates a dedicated Git worktree, and gives a bounded
execution contract to a harness such as Codex. It then tracks the run, meters
usage, preserves checkpoints, recovers managed sessions, and holds completed work
for review.

> **Project status:** executable local MVP. The scheduler, state store, Git lease
> management, managed Codex integration, and hardened single-host deployment
> profile are implemented. Execution is still local-process only. The deployment
> profile includes mandatory runtime isolation probes, but those host-dependent
> checks must pass on the target machine before its security guarantees apply.
> This is neither a distributed agent platform nor a general multi-tenant security
> boundary.

## At a glance

| Area | Current implementation |
| --- | --- |
| Scheduling | Deterministic QoS, priority, dependency, gang-readiness, capability, and best-fit logical node placement |
| State | SQLite snapshots, append-only job transitions and usage samples, durable commands, and schema version 1 |
| Workspaces | One owned Git branch and linked worktree per lease |
| Execution | One local-process backend; logical node placement does not provide remote execution |
| Harnesses | Deterministic fake driver, managed Codex SDK/App Server driver, and legacy `codex exec` driver |
| Interfaces | Python control-plane API, worker-scoped API, administrative CLI, and a polling daemon |
| Completion | Managed Codex uses a metered review handoff with optional repair turns; `agentd` never merges the result |

## What it does

`agentd` owns the control-plane concerns that should not be delegated to the model:

- accepts typed jobs with objectives, acceptance criteria, dependencies, effort
  estimates, QoS, resource requirements, capabilities, and quota budgets;
- orders ready work deterministically and admits it only when dependencies, quota,
  provider policy, compatible logical node metadata, a local worker backend, and
  a matching harness are available;
- uses separate atomic SQLite operations to reserve quota and logical CPU/RAM/GPU
  capacity against the selected node record;
- creates and validates exclusive Git worktree leases without mutating the base
  branch;
- starts a selected harness through a worker-backend protocol;
- gives the worker only its `ExecutionContract`, not quota balances, node identity,
  scarcity, QoS rank, or scheduler reasoning;
- supports durable checkpoints, suspension, logical continuation, usage
  accounting, managed Codex thread recovery, and bounded repair turns. Resume and
  restart recovery preserve durable intent and thread state; they do not resume
  the interrupted OS process or exact turn; and
- creates a trusted handoff commit for a valid managed Codex result, then waits for
  an operator to accept or repair it.

The harness owns execution of the assignment. It does not choose its priority,
resources, quota, workspace, or lifecycle.

## How a job moves

```text
CLI / Python caller
        |
        v
ControlPlane + AgentDaemon
        |
        +-- readiness -> quota -> logical placement -> Git lease
        |
        v
LocalWorkerBackend (current host only)
        |
        +-- fake | Codex SDK/App Server | legacy codex-cli
        |
        v
checkpoint / metering / review / repair / acceptance
```

In the current MVP, placement selects and accounts against a `WorkerNode` record.
It does not route execution to another machine. `LocalWorkerBackend` starts every
selected harness on the host running the controller process.

For an ordinary job, the main path is:

1. `submit` persists the job in `BACKLOG` and moves it to `READY`.
   Hors-categorie work goes to `PLANNING` and receives a bounded reconnaissance
   child instead.
2. The daemon filters incomplete dependencies and gang members, then orders the
   remaining candidates by QoS, priority, age, and stable job ID.
3. The coordinator reserves the expected quota path, selects a compatible logical
   node, accounts the requested resources against that node, and validates or
   creates the job's worktree lease.
4. The coordinator persists an admitted run before `LocalWorkerBackend` starts
   the selected driver on the current host. The selected node does not represent
   a remote execution target in the current MVP. Failed starts compensate acquired
   resources only after the process is known to be quiescent.
5. The driver receives a compact `ExecutionContract` containing the objective,
   scope, acceptance criteria, dependency handoffs, workspace, model class, and
   optional resume capsule.
6. Managed Codex runs stream observations and cumulative token usage into durable
   state. When no suspension is pending, valid terminal telemetry produces a
   trusted handoff commit and moves the job to `REVIEW`. A pending checkpoint or
   suspension leaves the completed attempt in `SUSPENDED` for operator review.
7. An operator accepts the result or requests one of at most two same-thread repair
   turns. Acceptance moves the job to `COMPLETED`; integration remains outside
   `agentd`.

The common managed lifecycle is:

```text
BACKLOG -> READY -> ADMITTED -> RUNNING -> REVIEW -> COMPLETED
             ^                    |          |
             |                    |          +-> RUNNING (repair) -> REVIEW
             +-- SUSPENDED <- CHECKPOINTED

RUNNING -> METERING_PENDING   when terminal usage cannot be trusted
```

Generic drivers may complete directly. Managed Codex work uses the explicit review
gate.

## Quick start without an LLM

You need:

- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/); and
- Git with `git worktree` support.

Install the locked environment and inspect the CLI:

```bash
uv sync --frozen
uv run agentd --help
```

The following example runs the deterministic fake driver. It uses a local Git
repository, creates real SQLite state and a real worktree lease, but does not start
Codex or contact an LLM:

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
            QuotaPool(id="default", provider="demo", remaining=100)
        )
        job = plane.submit(
            Job(
                project="example",
                repository=str(repository),
                objective="Implement and validate the bounded change",
                effort=EffortEstimate(p50=10, p90=20, p99=40),
                quota_budget=QuotaBudget(implementation=16),
                acceptance_criteria=("Tests pass",),
            )
        )

        run = await plane.dispatch_next()
        if run is None:
            raise RuntimeError("No job was dispatchable")

        worker = AgentAPI(plane)
        assignment = worker.get_assignment(run.id)
        result = await worker.complete(run.id)
        print(f"{assignment.objective}: {result.state.value}")


asyncio.run(main())
```

The Python API is the portable end-to-end fake path. The CLI can create and inspect
fake jobs, but it has no one-shot `dispatch` command and its production `serve`
composition enables the trusted Codex service policy.

## Use the CLI

The CLI administers durable state and runs the continuous service. Global
`--db`, `--workspace-root`, and `--codex-home` options must appear before the
subcommand.

### Initialize and inspect state

This sequence creates a local database, registers fake capacity and quota, and
queues a job. The job remains `READY` until a control-plane process dispatches it.

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
```

For production use with the current backend, register node metadata that represents
the controller host. Registering additional `WorkerNode` records does not create
remote workers; without a remote backend they remain scheduling and accounting
metadata.

### Run managed Codex work

The reviewed service path additionally requires Linux, Bubblewrap, unprivileged
user namespaces, and a dedicated authenticated Codex home. The primary `codex`
driver uses the pinned `openai-codex==0.144.4` SDK and its App Server runtime; it
does not need a separate `codex` executable on `PATH`. Only the optional
`codex-cli` driver does.

First provision the service paths and credentials described in
[the deployment guide](docs/deployment.md). Then run the read-only preflight with
the same paths that the service will use:

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  doctor
```

Register a token-denominated pool and compatible local node, then submit a Codex
job with a cumulative maximum. The current trusted provisioner requires the
repository to support `uv sync --frozen --extra dev --python 3.14`. The reviewed
container profile mounts Goldenage at `/home/bened/goldenage`; using another
repository requires a separately reviewed isolated mount policy.

```bash
uv run agentd --db .agentd/state.sqlite register-quota codex \
  --provider openai-codex-chatgpt --remaining 500000 \
  --interactive-reserve 50000 --unit tokens
uv run agentd --db .agentd/state.sqlite register-node local \
  --cpu 8 --ram-gb 16 --harness codex
uv run agentd --db .agentd/state.sqlite submit \
  --project example \
  --repository /home/bened/goldenage \
  --objective "Implement and validate the bounded change" \
  --p50 25000 --p90 75000 --p99 100000 \
  --quota 75000 --quota-maximum 100000 --quota-pool codex \
  --harness codex --model-class gpt-5.6-terra \
  --accept "Tests pass"
```

Start the foreground daemon with the same paths:

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  serve
```

`serve` recovers durable managed runs, polls Codex account telemetry, reconciles
live usage and commands, dispatches ready work, and stops on `SIGINT` or `SIGTERM`.
The trusted service composition currently fixes the model to `gpt-5.6-terra` and
reasoning effort to `medium`.

Inspect metering and decide reviewed work with:

```bash
CODEX_HOME=/absolute/path/to/dedicated-codex-home \
  uv run agentd --db .agentd/state.sqlite \
  codex-status --pool codex
uv run agentd --db .agentd/state.sqlite usage --job JOB_ID
uv run agentd --db .agentd/state.sqlite repair JOB_ID \
  --instruction "Address the review findings and rerun validation"
uv run agentd --db .agentd/state.sqlite accept JOB_ID
```

`codex-status` contacts App Server and persists the resulting provider snapshot;
`doctor` is the read-only command. `review` promotes a completed, quiescent
suspended checkpoint after external validation. Managed Codex completions with
valid metering enter `REVIEW` automatically when no suspension is pending.

The CLI intentionally exposes the common administrative subset. Advanced job
requirements, pause/resume/cancel, tail evaluation, reconnaissance promotion, and
quota reset events are Python `ControlPlane` operations.

## Configuration

`ServiceConfig` reads these environment variables. CLI path flags override the
corresponding path values.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTD_DB` | `$XDG_STATE_HOME/agentd/state.sqlite` | SQLite control-plane state |
| `AGENTD_WORKSPACE_ROOT` | `$XDG_DATA_HOME/agentd/workspaces` | Parent for leased Git worktrees |
| `AGENTD_CODEX_HOME` | `$XDG_DATA_HOME/agentd/codex-home` | Dedicated Codex authentication and session state |
| `UV_CACHE_DIR` | `$XDG_CACHE_HOME/uv` | Shared uv cache and managed Python toolchain |
| `AGENTD_CODEX_MODEL` | `gpt-5.6-terra` | SDK model; trusted `serve` rejects other values |
| `AGENTD_CODEX_REASONING_EFFORT` | `medium` | SDK effort; trusted `serve` rejects other values |
| `AGENTD_POLL_SECONDS` | `1` | Daemon polling interval |
| `AGENTD_ACCOUNT_POLL_SECONDS` | `60` | Provider telemetry refresh interval |
| `AGENTD_ACCOUNT_STALE_SECONDS` | `300` | Age after which telemetry restricts admission |
| `AGENTD_QUOTA_TOP_UP_TOKENS` | `25000` | Increment used to extend an active reservation |
| `AGENTD_HARD_CAP_GRACE_SECONDS` | `120` | Grace period before a hard-cap interrupt |
| `AGENTD_LOG_LEVEL` | `INFO` | Loguru level |
| `AGENTD_LOG_FORMAT` | `json` | `json` for production or `text` for local diagnosis |

When an XDG variable is unset, paths fall back under `~/.local/state`,
`~/.local/share`, and `~/.cache`. Numeric policy values must be positive.

## State, isolation, and recovery

- **SQLite owns runtime truth:** job snapshots and transitions, nodes and
  allocations, quota pools and reservations, workspaces, runs and contracts,
  checkpoints, managed sessions and observations, provider snapshots, usage
  samples, and command acknowledgements. File databases use foreign keys, a busy
  timeout, and WAL mode. Version 0 databases initialize to schema version 1;
  databases from a newer version are rejected.
- **Git owns artifacts:** each lease gets an owned branch and linked worktree.
  Validation checks repository identity, branch ownership, Git registration, and
  confinement beneath the workspace root. Normal release is not forced. Dirty
  output is retained for inspection, and worker branches are not deleted.
- **The repository owns intent:** callers currently construct jobs, dependencies,
  acceptance criteria, and reconnaissance outcomes. `agentd` does not infer them
  from issues or plans.
- **Managed recovery resumes intent, not a process:** after restart, the Codex SDK
  driver starts a new App Server process, resumes the durable thread in the
  validated worktree, and begins a recovery turn. It cannot reattach the old stdio
  transport or continue the exact interrupted turn.

## What it cannot do

These are current boundaries, not hidden roadmap claims:

- It supports one active scheduling daemon per SQLite database. Administrative CLI
  processes may open the same database, but multiple concurrent schedulers are
  unsupported: there is no leader election, distributed ownership protocol, or
  transactional outbox.
- It cannot execute on a registered machine remotely. `WorkerNode` is scheduling
  metadata; the only worker backend starts a harness on the current host.
- It has no HTTP/JSON service, MCP transport, web UI, SSH backend, Kubernetes,
  Slurm, or generic container/cloud worker backend. The reviewed container runs the
  single-host service; it is not a container-worker abstraction.
- It has no Claude, OpenClaw, or llama.cpp adapter. The fake driver is for tests and
  demos; the two real adapters are managed Codex and legacy local `codex exec`.
- It does not read Beads, issues, or planning documents and cannot compile
  repository intent into jobs automatically.
- Gang readiness can hold related jobs until their members are dependency-ready.
  There is no distributed gang launch or multi-host barrier: every dispatched
  harness still starts through the local-process backend. Gang barrier state is
  not persisted as a separate aggregate.
- Tail-governor decisions are a Python policy call, not an automatic daemon loop.
  Explicit quota reset events also require a caller.
- Refinement and blocker messages are bounded in-memory records and are lost on
  restart. Checkpoints, transitions, usage, and managed commands are durable.
- The legacy CLI driver and fake process objects do not have managed restart or
  live token metering. Managed Codex recovery starts a new turn rather than
  restoring the interrupted transport.
- Provider percentages and opaque credits are admission signals. `agentd` cannot
  convert them into an absolute local token balance.
- Invalid terminal telemetry leaves the job in `METERING_PENDING`; there is no
  automatic provider-side settlement for that state.
- It does not provide an automated reviewer, merge queue, or integration policy.
  A trusted handoff commit records an artifact but does not accept or merge it.
- The trusted provisioning profile currently installs Python 3.14 and the `dev`
  extra for the reviewed deployment. Per-job toolchains and extras are not a
  public policy surface.
- SQLite records schema version 1 in `PRAGMA user_version`. Databases with version
  0 are bootstrapped in place to the current schema; databases newer than the
  binary are rejected. No version-to-version upgrade migration exists yet.

## Troubleshooting

**Why is a job still `READY`?** A candidate is skipped when a dependency or gang
member is not ready, its quota pool is missing or insufficient, no online node
matches its harness/model/resources/capabilities, provider policy blocks its QoS,
or no matching backend exists. Repository and base-ref validity are checked when
the workspace is allocated, not when the job is submitted. Inspect `job`, `nodes`,
and `quota`, then check daemon logs for a compensated dispatch failure.

**Why does `doctor` fail on a development machine?** `doctor` validates the
managed service profile, including Bubblewrap, nested user namespaces, the pinned
SDK, writable service paths, a mode-`0700` Codex home, and a mode-`0600`
`auth.json`. Those checks are intentionally stricter than the fake-driver
quickstart.

**Why is a job in `METERING_PENDING`?** The terminal turn did not produce a valid,
matching usage sample. `agentd` releases node capacity but holds the quota
reservation and blocks acceptance instead of guessing a charge.

**Where did a dirty worktree go?** It was retained. Normal cleanup never forces
removal of uncommitted worker output.

## Development

The pass/fail local checks are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=agentd --cov-report=term-missing
```

Run the separate complexity inventory with:

```bash
uv run ruff check src/agentd --select C901
```

This inventory currently exits nonzero with 11 documented lifecycle hotspots.
Treat that count as the preservation baseline in
[the remediation notes](docs/review-remediation.md), not as a pass/fail gate.

The test suite uses temporary Git repositories, SQLite, fake nodes and quota, and
fake processes. It does not invoke an LLM or a real Codex process.

## Further reading

- [Architecture and lifecycle boundaries](docs/architecture.md)
- [Reviewed single-host deployment and rollback](docs/deployment.md)
- [Operational-readiness preservation contract](docs/review-remediation.md)
- [Security policy and trust boundary](SECURITY.md)
- [License](LICENSE)
