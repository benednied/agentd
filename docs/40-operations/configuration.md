# Configuration

`ServiceConfig` derives portable defaults from `HOME` and XDG directories. CLI
path flags override the corresponding environment-derived values.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTD_DB` | `$XDG_STATE_HOME/agentd/state.sqlite` | SQLite control-plane state |
| `AGENTD_WORKSPACE_ROOT` | `$XDG_DATA_HOME/agentd/workspaces` | Parent for leased Git worktrees |
| `AGENTD_CODEX_HOME` | `$XDG_DATA_HOME/agentd/codex-home` | Dedicated Codex authentication and session state |
| `UV_CACHE_DIR` | `$XDG_CACHE_HOME/uv` | Shared uv cache and managed toolchain |
| `AGENTD_CODEX_MODEL` | `gpt-5.6-terra` | SDK model; trusted `serve` enforces production policy |
| `AGENTD_CODEX_REASONING_EFFORT` | `medium` | SDK effort; trusted `serve` enforces production policy |
| `AGENTD_POLL_SECONDS` | `1` | Daemon polling interval |
| `AGENTD_ACCOUNT_POLL_SECONDS` | `60` | Provider telemetry refresh interval |
| `AGENTD_ACCOUNT_STALE_SECONDS` | `300` | Age after which telemetry restricts admission |
| `AGENTD_QUOTA_TOP_UP_TOKENS` | `25000` | Increment for extending an active reservation |
| `AGENTD_HARD_CAP_GRACE_SECONDS` | `120` | Grace period before a hard-cap interrupt |
| `AGENTD_LOG_LEVEL` | `INFO` | Loguru level |
| `AGENTD_LOG_FORMAT` | `json` | `json` for production or `text` for diagnosis |

Unset XDG variables fall back under `~/.local/state`, `~/.local/share`, and
`~/.cache`. Numeric policy values must be positive.

The configuration type supports non-production model and reasoning choices. The
trusted production composition, not the portable configuration type, enforces the
reviewed model policy.
