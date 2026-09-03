# Limitations

These are current product boundaries. This repository does not publish a roadmap;
the limitations below are not commitments to future capabilities.

- One active scheduling daemon is supported per SQLite database. There is no
  leader election, distributed ownership protocol, or transactional outbox.
- Registered worker nodes are logical scheduling metadata until a compatible
  backend is configured. The MVP has an authenticated remote worker protocol for
  typed artifact Build/Deploy operations only. It is not general remote code-agent
  execution, and it has no SSH, MCP, Kubernetes, Slurm, or cloud scheduler.
- There is no web UI, merge queue, automated reviewer, or integration policy.
- Real adapters are managed Codex and legacy local `codex exec`; there are no
  Claude, OpenClaw, or llama.cpp adapters.
- `agentd` does not read Beads, issues, or planning documents to infer jobs.
- Gang readiness can hold related jobs, but there is no distributed gang launch
  or persisted gang-barrier aggregate.
- The daemon applies the tail governor and provider reset detection during its
  polling loop. The Python API remains available for explicit policy evaluation
  and reset-event registration.
- Refinement and blocker messages are bounded durable Agent Requests. Checkpoints,
  transitions, usage, reset events, and managed commands are durable as well.
- Managed Codex recovery resumes durable intent on a new App Server process and
  turn; it cannot restore an interrupted OS process or exact turn. A remote worker
  process that disappears or restarts is fail-closed: its in-flight operation is
  not magically continued.
- There is no HA control plane, leader election, Kubernetes integration, or web UI.
- `deploy.sh` remains the legacy reviewed single-host release/rollback path; it
  does not deploy the remote worker fleet.
- Detached config worktrees used by active Compose projects are retained for
  rollback and host-path stability; this MVP does not prune them automatically.
- Provider percentages and opaque credits are admission signals, not absolute
  local token balances.
- Invalid terminal telemetry leaves a job in `METERING_PENDING`; there is no
  automatic provider-side settlement.
- The trusted deployment currently fixes Python 3.14, the `dev` extra, model
  `gpt-5.6-terra`, and reasoning effort `medium` at the production boundary.
- SQLite schema version 4 bootstraps version 0 as version 1 and applies the
  explicit `v1 -> v2 -> v3 -> v4` migration chain. Future schema changes still
  require an idempotent migration before the version is increased.
