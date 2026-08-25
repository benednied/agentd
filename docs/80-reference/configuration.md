# Configuration reference

The following environment variables are read by `ServiceConfig`. CLI path flags
override `AGENTD_DB`, `AGENTD_WORKSPACE_ROOT`, and `AGENTD_CODEX_HOME`. Values are
validated when configuration is constructed.

| Variable | Default | Type / policy |
| --- | --- | --- |
| `HOME` | platform home | base path for XDG fallbacks |
| `XDG_STATE_HOME` | `$HOME/.local/state` | base path |
| `XDG_DATA_HOME` | `$HOME/.local/share` | base path |
| `XDG_CACHE_HOME` | `$HOME/.cache` | base path |
| `AGENTD_DB` | `$XDG_STATE_HOME/agentd/state.sqlite` | filesystem path |
| `AGENTD_WORKSPACE_ROOT` | `$XDG_DATA_HOME/agentd/workspaces` | filesystem path |
| `AGENTD_CODEX_HOME` | `$XDG_DATA_HOME/agentd/codex-home` | filesystem path |
| `UV_CACHE_DIR` | `$XDG_CACHE_HOME/uv` | filesystem path |
| `AGENTD_CODEX_MODEL` | `gpt-5.6-terra` | string |
| `AGENTD_CODEX_REASONING_EFFORT` | `medium` | string |
| `AGENTD_POLL_SECONDS` | `1` | finite positive number |
| `AGENTD_ACCOUNT_POLL_SECONDS` | `60` | finite positive number |
| `AGENTD_ACCOUNT_STALE_SECONDS` | `300` | finite positive number |
| `AGENTD_QUOTA_TOP_UP_TOKENS` | `25000` | finite positive number |
| `AGENTD_HARD_CAP_GRACE_SECONDS` | `120` | finite positive number |
| `AGENTD_LOG_LEVEL` | `INFO` | supported Loguru level |
| `AGENTD_LOG_FORMAT` | `json` | `json` or `text` |

The trusted production composition enforces model `gpt-5.6-terra` and reasoning
effort `medium`. The portable configuration can represent other values for tests
and local integrations.
