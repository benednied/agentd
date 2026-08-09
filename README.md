# agentd

`agentd` is a Python control plane for scheduling AI coding agents across harnesses
and execution environments. It sits above the harness: Codex or a fake driver
executes a bounded assignment, while `agentd` owns readiness, placement, quota,
workspace isolation, preemption, and lifecycle.

> Agents execute work. The control plane owns scheduling, resources, isolation,
> quota, preemption, and lifecycle.

This repository is an executable local MVP, not a distributed production service.
The implemented backend is a local process; SSH, containers, Kubernetes, Slurm,
Claude, OpenClaw, and llama.cpp are extension points rather than current adapters.

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
- Pure tail and reset/burn policies. Tail decisions are exposed through the Python
  API; there is not yet a usage-monitor loop that applies them automatically.
- Exclusive Git branches/worktrees, lease validation, resume capsules, suspension,
  review, completion, and immutable commit fields for dependency handoffs.
- Fake and Codex harness drivers behind a capability protocol, plus a local worker
  backend.
- A Python control-plane facade, run-scoped model API, administrative CLI, and
  embeddable dispatch loop.

The detailed component, lifecycle, and durability boundaries are in
[docs/architecture.md](docs/architecture.md).

## Requirements and installation

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git, including `git worktree`
- The [`codex` executable](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
  only when using `CodexDriver`

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

## Administrative CLI

The CLI initializes and inspects SQLite state and can submit jobs, nodes, and quota
pools:

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

The CLI is administrative only. It does not compose drivers/workspaces and has no
dispatch, pause, resume, cancel, review, daemon, or reset-event subcommand. Use
`create_local_runtime()` and `ControlPlane` for those lifecycle operations.

## Fake and Codex drivers

`FakeHarnessDriver` is deterministic and in-process. It returns a configurable
`RunResult`, records calls, and is intended for scheduler/application tests and
safe local demonstrations.

`create_local_runtime()` registers both fake and Codex drivers by default. To run a
Codex job, the local `codex` binary must be installed, on `PATH`, and already
authenticated/configured. Register a node with `harnesses={"codex"}` and submit a
job with both `preferred_harnesses` and `allowed_harnesses` set to `("codex",)`.

`CodexDriver` launches `codex exec --json` without a shell, parses JSONL, and can
deliver queued steering through `codex exec resume` when a session ID is reported.
On POSIX, each run owns a new process session; completion, interruption, and
cancellation terminate the full process group before resources are released.
The default abstract model class, `standard`, leaves model choice to the user's
Codex configuration. To opt into an explicit mapping:

```python
from agentd.harness import CodexDriver

runtime.drivers.register(
    CodexDriver(
        models=frozenset({"standard", "premium"}),
        model_aliases={"premium": "your-codex-model"},
    ),
    replace=True,
)
```

Live driver process/session objects are in memory. Persisted run handles are useful
for audit, but the MVP cannot reattach to an active Codex process after restart.

## State ownership

The architecture treats the source repository as authoritative for issues/Beads,
plans, dependencies, acceptance criteria, review relationships, and accepted
decisions. The current MVP does not yet read or write Beads or compile repository
plans automatically: callers create `Job` snapshots and structured reconnaissance
results through the Python API.

SQLite owns control-plane execution records: job snapshots and transitions, nodes,
resource allocations, quota pools/reservations, workspaces, runs/contracts, and
checkpoints. Git owns source files, worker branches, commits, and uncommitted worker
output. Agentd never merges a worker branch into the integration branch.

## MVP limitations

- One local process, one SQLite database, one local-process worker backend, and no
  distributed leader election or outbox/reconciler.
- No HTTP/JSON service, MCP transport, web UI, SSH/container/cloud backend, or
  external quota-reset watcher.
- No startup recovery or reattachment for live harness processes. SQLite retains
  the run record, contract, and checkpoint, but not a usable process object.
- Process-tree ownership is implemented for the local POSIX Codex path; a Windows
  job-object implementation remains a future worker-backend concern.
- Dependency and gang readiness are implemented, but gang launch is not atomic and
  there is no persisted barrier aggregate.
- Tail decisions and quota-reset events require an external caller; quota is an
  abstract numeric estimate rather than live provider metering.
- `request_refinement` and `report_blocker` are not durable across process restart.
- SQLite creates its schema in place; there is no versioned migration system yet.
- There is no automated reviewer/integrator. `REVIEW` and acceptance are lifecycle
  operations invoked by a caller.
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
  harness/       driver protocol, fake driver, and Codex adapter
  workers/       execution-backend protocol and local backend
  coordinator.py effectful admission, compensation, and lifecycle sequencing
  service.py     transport-independent control-plane facade
  agent_api.py   run-scoped model-facing protocol
  daemon.py      asynchronous admission/dispatch loop
```
