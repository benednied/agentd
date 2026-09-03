# agentd

**A local-first control plane for AI coding agents.**

`agentd` decides which job may run, reserves quota and capacity, and gives a
bounded execution contract to a worker. It tracks the run, meters usage,
preserves checkpoints, recovers managed sessions, and holds completed work for
review. `agentd` does not write code or merge changes.

## Status

The repository contains an executable MVP with SQLite state, deterministic
scheduling, Git worktree leases, managed Codex integration, and an authenticated
remote artifact-worker path. Remote workers execute only typed Build/Deploy
operations with immutable artifact references; ordinary coding-agent runs remain
local. The project is not a general remote-code-agent platform or a general
multi-tenant security boundary.

## How it works

```text
Caller / repository intent
          |
          v
ControlPlane + AgentDaemon
          |
readiness -> quota -> placement -> local Git lease or typed artifact contract
          |
          +-> LocalWorkerBackend -> fake | Codex SDK/App Server | codex-cli
          `-> RemoteWorkerBackend -> authenticated worker-serve (Build/Deploy)
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
- exclusive Git branch and linked-worktree leases for local coding runs;
- fake, managed Codex SDK/App Server, and legacy `codex exec` harnesses;
- authenticated HMAC+TLS worker protocol with durable operation journal and
  persisted worker-heartbeat status;
- worker-side allowlisted Git/OCI caches and digest-pinned Docker Compose Deploy;
- immutable artifact references, verified outputs, durable Agent Requests, and
  exactly-once provider reset events;
- durable checkpoints, usage samples, managed-session recovery, and bounded repair;
- provider reset detection/ledger and a fixed hard stop at the final 2% of
  fresh provider quota;
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
