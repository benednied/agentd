# Limitations

These are current product boundaries. This repository does not publish a roadmap;
the limitations below are not commitments to future capabilities.

- One active scheduling daemon is supported per SQLite database. There is no
  leader election, distributed ownership protocol, or transactional outbox.
- Registered worker nodes are logical scheduling metadata. Execution is local;
  there is no SSH, HTTP/JSON, MCP, Kubernetes, Slurm, cloud, or remote backend.
- There is no web UI, merge queue, automated reviewer, or integration policy.
- Real adapters are managed Codex and legacy local `codex exec`; there are no
  Claude, OpenClaw, or llama.cpp adapters.
- `agentd` does not read Beads, issues, or planning documents to infer jobs.
- Gang readiness can hold related jobs, but there is no distributed gang launch
  or persisted gang-barrier aggregate.
- Tail-governor decisions and quota-reset events require an explicit Python call.
- Refinement and blocker messages are bounded in-memory records and are lost on
  restart. Checkpoints, transitions, usage, and managed commands are durable.
- Managed recovery resumes durable intent on a new App Server process and turn;
  it cannot restore an interrupted OS process or exact turn.
- Provider percentages and opaque credits are admission signals, not absolute
  local token balances.
- Invalid terminal telemetry leaves a job in `METERING_PENDING`; there is no
  automatic provider-side settlement.
- The trusted deployment currently fixes Python 3.14, the `dev` extra, model
  `gpt-5.6-terra`, and reasoning effort `medium` at the production boundary.
- SQLite schema version 1 supports bootstrap from version 0, but no general
  version-to-version migration chain exists yet.
