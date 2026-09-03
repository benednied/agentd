# CLI reference

The CLI is built by `agentd.cli.build_parser()`. Global options must appear before
the subcommand. The documentation check parses every example in this page against
that parser.

```bash
uv run agentd --help
uv run agentd COMMAND --help
```

## Global options

| Option | Default | Meaning |
| --- | --- | --- |
| `--db PATH` | `ServiceConfig.database` | SQLite control-plane database |
| `--workspace-root PATH` | `ServiceConfig.workspace_root` | Parent for Git worktrees |
| `--codex-home PATH` | `ServiceConfig.codex_home` | Dedicated Codex home |

## Commands

### State and inspection

```bash
uv run agentd --db PATH init
uv run agentd --db PATH jobs
uv run agentd --db PATH job JOB_ID
uv run agentd --db PATH history JOB_ID
uv run agentd --db PATH nodes
uv run agentd --db PATH quota POOL_ID
uv run agentd --db PATH usage --job JOB_ID
uv run agentd --db PATH usage --run RUN_ID
```

| Command | Positional arguments | Options |
| --- | --- | --- |
| `init` | none | none |
| `jobs` | none | none |
| `job` | `job_id` | none |
| `history` | `job_id` | none |
| `nodes` | none | none |
| `quota` | `pool_id` | none |
| `usage` | none | exactly one of `--job JOB_ID`, `--run RUN_ID` |

### Register a quota pool

```bash
uv run agentd --db PATH register-quota codex \
  --provider openai-codex-chatgpt \
  --remaining 500000 \
  --interactive-reserve 50000 \
  --unit tokens
```

| Option | Required | Default / choices |
| --- | --- | --- |
| `pool_id` | yes | positional |
| `--provider PROVIDER` | yes | — |
| `--remaining AMOUNT` | yes | finite number |
| `--interactive-reserve AMOUNT` | no | `0` |
| `--unit UNIT` | no | `abstract`, `tokens` |

### Register a worker node

```bash
uv run agentd --db PATH register-node local \
  --os linux --arch x86_64 --cpu 8 --ram-gb 16 \
  --gpu-count 0 --vram-gb 0 --harness codex \
  --capability local-process
```

| Option | Required | Default / choices |
| --- | --- | --- |
| `node_id` | yes | positional |
| `--os OS` | no | current platform |
| `--arch ARCH` | no | current architecture |
| `--cpu CPU` | yes | finite number |
| `--ram-gb RAM` | yes | finite number |
| `--gpu-count COUNT` | no | `0` |
| `--vram-gb GB` | no | `0` |
| `--harness HARNESS` | yes | repeatable |
| `--capability CAPABILITY` | no | repeatable |

### Submit a job

```bash
uv run agentd --db PATH submit \
  --project example \
  --repository REPOSITORY \
  --base-ref HEAD \
  --objective "Implement and validate the bounded change" \
  --qos normal --priority 0 \
  --p50 25000 --p90 75000 --p99 100000 \
  --quota 75000 --quota-maximum 100000 --quota-pool codex \
  --harness codex --model-class gpt-5.6-terra \
  --accept "Tests pass" --depends-on DEPENDENCY_ID
```

| Option | Required | Default / behavior |
| --- | --- | --- |
| `--project PROJECT` | yes | project name |
| `--repository PATH` | yes | Git repository |
| `--base-ref REF` | no | `HEAD` |
| `--objective TEXT` | yes | caller-supplied objective |
| `--qos CLASS` | no | `normal`; enum value |
| `--priority INTEGER` | no | `0` |
| `--p50 NUMBER` | yes | effort estimate |
| `--p90 NUMBER` | yes | effort estimate |
| `--p99 NUMBER` | no | effort estimate |
| `--quota NUMBER` | yes | implementation quota |
| `--quota-maximum NUMBER` | no | cumulative maximum |
| `--quota-pool POOL_ID` | no | `default` |
| `--harness HARNESS` | no | repeatable allowed harness |
| `--model-class CLASS` | no | selected model class constraint |
| `--accept TEXT` | no | repeatable acceptance criterion |
| `--depends-on JOB_ID` | no | repeatable dependency |

### Service and managed review

```bash
uv run agentd --db PATH --workspace-root PATH --codex-home PATH serve
uv run agentd --db PATH --workspace-root PATH --codex-home PATH doctor
CODEX_HOME=PATH uv run agentd --db PATH codex-status --pool codex --bucket primary
uv run agentd --db PATH usage --job JOB_ID
uv run agentd --db PATH repair JOB_ID --instruction "Address the review findings"
uv run agentd --db PATH accept JOB_ID
uv run agentd --db PATH review JOB_ID
```

`doctor` is read-only. `serve` recovers managed runs, polls account telemetry,
reconciles commands, dispatches ready work, and stops on `SIGINT` or `SIGTERM`.
`review` promotes a completed, quiescent suspended checkpoint after operator
validation. `accept` completes a job already in `REVIEW`.

Pause/resume/cancel, tail evaluation, reconnaissance promotion, and quota-reset
operations are Python `ControlPlane` operations rather than CLI commands.

### Remote artifact worker

`worker-serve` starts one authenticated, node-bound worker process. It exposes only
the typed worker protocol and the allowlisted Build/Deploy operation driver.

```bash
AGENTD_WORKER_PSK_FILE=/secure/worker.psk \
uv run agentd worker-serve \
  --host 0.0.0.0 --port 8765 \
  --node-id build-node-1 --session-epoch 2026-08-31T12:00Z \
  --journal /var/lib/agentd/worker-journal.sqlite \
  --tls-cert /etc/agentd/worker.crt \
  --tls-key /etc/agentd/worker.key \
  --operations-config /etc/agentd/operations.json \
  --cache-root /var/lib/agentd/cache \
  --operation-state-root /var/lib/agentd/operations
```

| Option | Required | Meaning |
| --- | --- | --- |
| `--host`, `--port` | no | bind address and port (environment defaults apply) |
| `--node-id`, `--session-epoch` | yes unless supplied by environment | durable worker identity and epoch |
| `--journal` | yes unless supplied by environment | operation journal path |
| `--psk-file` | one PSK source | mode-`0600` pre-shared-key file |
| `--tls-cert`, `--tls-key` | paired by default | TLS certificate and key |
| `--operations-config` | no | strict operation allowlist document |
| `--cache-root`, `--operation-state-root` | no | worker cache and deployment-state roots |
| `--allow-insecure-loopback` | no | explicit plaintext exception restricted to loopback |

The CLI worker command does not deploy a fleet and does not enable general remote
code-agent execution. Configure a matching remote backend in the embedding
control-plane runtime; the standalone administrative CLI does not infer one from
`register-node`.
