# Getting started

## Requirements

- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/); and
- Git with `git worktree` support.

Install the locked environment and inspect the CLI:

```bash
uv sync --frozen
uv run agentd --help
```

## Fake-driver quickstart

The fake driver is deterministic and requires no LLM account. It still exercises
the real SQLite state store, scheduler, quota accounting, and Git workspace
lease. Initialize a local database, capacity, and quota:

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
```

The CLI queues and inspects durable state. The continuous `serve` command
dispatches work and runs the daemon; the portable Python API below is the
smallest complete fake execution path.

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

The Python API is the portable end-to-end fake path. It does not start Codex or
contact an LLM.

## Managed Codex path

The reviewed service path requires Linux, Bubblewrap, unprivileged user
namespaces, and a dedicated authenticated Codex home. Follow the [deployment
guide](../40-operations/deployment.md), then run the read-only preflight with the
same paths that the service will use:

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  doctor
```

Register a token-denominated pool and a compatible local node:

```bash
uv run agentd --db .agentd/state.sqlite register-quota codex \
  --provider openai-codex-chatgpt \
  --remaining 500000 \
  --interactive-reserve 50000 \
  --unit tokens
uv run agentd --db .agentd/state.sqlite register-node local \
  --cpu 8 --ram-gb 16 --harness codex
```

Submit a job with a cumulative maximum. The reviewed container profile mounts the
repository at `/home/bened/goldenage`; another repository requires its own reviewed
mount policy.

```bash
uv run agentd --db .agentd/state.sqlite submit \
  --project example \
  --repository /home/bened/goldenage \
  --objective "Implement and validate the bounded change" \
  --p50 25000 --p90 75000 --p99 100000 \
  --quota 75000 --quota-maximum 100000 --quota-pool codex \
  --harness codex --model-class gpt-5.6-terra \
  --accept "Tests pass"
```

Start the foreground daemon and inspect the managed run:

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  serve

CODEX_HOME=/absolute/path/to/dedicated-codex-home \
  uv run agentd --db .agentd/state.sqlite \
  codex-status --pool codex
uv run agentd --db .agentd/state.sqlite usage --job JOB_ID
```

If review requests a repair, use one of at most two same-thread repair turns and
then explicitly accept the result:

```bash
uv run agentd --db .agentd/state.sqlite repair JOB_ID \
  --instruction "Address the review findings and rerun validation"
uv run agentd --db .agentd/state.sqlite accept JOB_ID
```

Managed completion creates a trusted handoff commit and waits in `REVIEW`; agentd
does not merge the result. The [CLI reference](../80-reference/cli.md) contains the
complete option surface.
