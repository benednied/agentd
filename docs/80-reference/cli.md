# CLI reference

Global `--db`, `--workspace-root`, and `--codex-home` options appear before the
subcommand. Run `uv run agentd --help` or `uv run agentd <command> --help` for the
current parser output.

## State and inspection

```bash
uv run agentd --db PATH init
uv run agentd --db PATH jobs
uv run agentd --db PATH job JOB_ID
uv run agentd --db PATH history JOB_ID
uv run agentd --db PATH nodes
uv run agentd --db PATH quota
uv run agentd --db PATH usage --job JOB_ID
```

## Registration and submission

```bash
uv run agentd --db PATH register-quota POOL \
  --provider PROVIDER --remaining AMOUNT
uv run agentd --db PATH register-node NODE \
  --cpu CPU --ram-gb RAM --harness HARNESS
uv run agentd --db PATH submit \
  --project PROJECT --repository PATH --objective TEXT \
  --p50 N --p90 N --p99 N --quota N --accept TEXT
```

Submission supports the advanced job requirements exposed by the Python
`ControlPlane` API; the CLI intentionally exposes the common administrative subset.

## Service and managed review

```bash
uv run agentd --db PATH --workspace-root PATH --codex-home PATH serve
uv run agentd --db PATH --codex-home PATH doctor
CODEX_HOME=PATH uv run agentd --db PATH codex-status --pool POOL
uv run agentd --db PATH repair JOB_ID --instruction TEXT
uv run agentd --db PATH accept JOB_ID
uv run agentd --db PATH review JOB_ID
```

`doctor` is read-only. `serve` recovers managed runs, polls account telemetry,
reconciles commands, dispatches ready work, and stops on `SIGINT` or `SIGTERM`.
Advanced pause/resume/cancel, tail evaluation, reconnaissance promotion, and
quota-reset operations are Python `ControlPlane` calls.
