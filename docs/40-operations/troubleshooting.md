# Troubleshooting

## Readiness and dispatch

If a job remains `READY`, inspect dependencies, gang members, quota pools, nodes,
harness/model compatibility, resource capacity, provider policy, and daemon logs.
Repository and base-ref validity are checked during workspace allocation, not at
submission. A compensated dispatch failure is reported in the lifecycle logs.

## Preflight

`doctor` validates the managed service profile, including Bubblewrap, nested user
namespaces, the pinned SDK, writable service paths, a mode-`0700` Codex home, and a
mode-`0600` `auth.json`. These checks are intentionally stricter than the fake
quickstart.

```bash
uv run agentd \
  --db .agentd/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  --codex-home /absolute/path/to/dedicated-codex-home \
  doctor
```

## Metering and worktrees

- `METERING_PENDING` means terminal usage was absent, inconsistent, or not present
  in the durable ledger. Capacity is released, but acceptance is blocked.
- A dirty worktree is retained for inspection. Normal cleanup never forces removal
  of uncommitted worker output.
- `serve` recovers durable managed runs, polls account telemetry, reconciles
  commands, dispatches ready work, and stops on `SIGINT` or `SIGTERM`.

For deployment-specific failures, inspect the [security verification](security.md)
and [deployment](deployment.md) requirements.
