# Configuration reference

The following environment variables are read by `ServiceConfig`. CLI path flags
override path variables.

| Variable | Default | Type / policy |
| --- | --- | --- |
| `AGENTD_DB` | `$XDG_STATE_HOME/agentd/state.sqlite` | filesystem path |
| `AGENTD_WORKSPACE_ROOT` | `$XDG_DATA_HOME/agentd/workspaces` | filesystem path |
| `AGENTD_CODEX_HOME` | `$XDG_DATA_HOME/agentd/codex-home` | filesystem path |
| `UV_CACHE_DIR` | `$XDG_CACHE_HOME/uv` | filesystem path |
| `AGENTD_CODEX_MODEL` | `gpt-5.6-terra` | string |
| `AGENTD_CODEX_REASONING_EFFORT` | `medium` | string |
| `AGENTD_POLL_SECONDS` | `1` | positive number |
| `AGENTD_ACCOUNT_POLL_SECONDS` | `60` | positive number |
| `AGENTD_ACCOUNT_STALE_SECONDS` | `300` | positive number |
| `AGENTD_QUOTA_TOP_UP_TOKENS` | `25000` | positive integer |
| `AGENTD_HARD_CAP_GRACE_SECONDS` | `120` | positive number |
| `AGENTD_LOG_LEVEL` | `INFO` | Loguru level |
| `AGENTD_LOG_FORMAT` | `json` | `json` or `text` |

Unset XDG variables use `~/.local/state`, `~/.local/share`, and `~/.cache`.
Portable configuration can represent non-production model values. Trusted
production composition enforces the reviewed model and reasoning policy.
