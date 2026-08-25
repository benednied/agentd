# agentd

**A local-first control plane for AI coding agents.**

`agentd` decides which job may run, reserves quota and local capacity, creates an
exclusive Git worktree, and gives a bounded execution contract to a harness such
as Codex. It tracks the run, meters usage, preserves checkpoints, recovers managed
sessions, and holds completed work for review. `agentd` does not write code or
merge changes.

## Status

The repository contains an executable local MVP with SQLite state, deterministic
scheduling, Git worktree leases, managed Codex integration, and a hardened
single-host deployment profile. Execution is local-process only: registered
worker nodes are scheduling and accounting metadata, not remote machines. The
project is not a distributed agent platform or a general multi-tenant security
boundary.

## How it works

```text
Caller / repository intent
          |
          v
ControlPlane + AgentDaemon
          |
readiness -> quota -> placement -> Git lease
          |
          v
LocalWorkerBackend -> fake | Codex SDK/App Server | codex-cli
          |
checkpoint -> metering -> review -> repair / acceptance
```

## Quick start

Requirements: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and Git.

```bash
uv sync --frozen
uv run agentd --help
uv run agentd --db .agentd/state.sqlite init
uv run agentd --db .agentd/state.sqlite register-quota default \
  --provider local-test --remaining 100
uv run agentd --db .agentd/state.sqlite register-node local \
  --cpu 8 --ram-gb 16 --harness fake
```

The deterministic fake path creates real SQLite state and Git worktrees without
starting Codex or contacting an LLM. See [Getting started](docs/10-start-here/getting-started.md)
for the complete CLI and Python examples.

## What it provides

- deterministic readiness, QoS, dependency, capability, and best-fit placement;
- atomic quota and CPU/RAM/GPU accounting in SQLite;
- exclusive Git branch and linked-worktree leases;
- fake, managed Codex SDK/App Server, and legacy `codex exec` harnesses;
- durable checkpoints, usage samples, managed-session recovery, and bounded repair;
- an explicit review gate; `agentd` never accepts or merges a result automatically.

See [current limitations](docs/10-start-here/limitations.md) for the supported
boundaries and [concepts](docs/10-start-here/concepts.md) for the system model.

## Documentation

- [Start here](docs/10-start-here/)
- [Using agentd](docs/20-using-agentd/)
- [Architecture](docs/30-architecture/)
- [Operations](docs/40-operations/)
- [Development](docs/50-development/)
- [Architecture decisions](docs/70-decisions/)
- [Reference](docs/80-reference/)
- [Archive](docs/90-archive/)

The root [security policy](SECURITY.md) defines the reporting process and trust
boundary. See the [license](LICENSE) for redistribution terms.
