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
| `AGENTD_WORKER_HEARTBEAT_SECONDS` | `15` | finite positive number |
| `AGENTD_ACCOUNT_POLL_SECONDS` | `60` | finite positive number |
| `AGENTD_ACCOUNT_STALE_SECONDS` | `300` | finite positive number |
| `AGENTD_QUOTA_TOP_UP_TOKENS` | `25000` | finite positive number |
| `AGENTD_HARD_CAP_GRACE_SECONDS` | `120` | finite positive number |
| `AGENTD_PROVIDER_STOP_REMAINING_FRACTION` | `0.02` | fixed safety boundary; values other than `0.02` are rejected |
| `AGENTD_PROVIDER_RESET_REMAINING` | unset | optional finite non-negative amount |
| `AGENTD_LOG_LEVEL` | `INFO` | supported Loguru level |
| `AGENTD_LOG_FORMAT` | `json` | `json` or `text` |

The trusted production composition enforces model `gpt-5.6-terra` and reasoning
effort `medium`. The portable configuration can represent other values for tests
and local integrations.

## Remote worker controller

`AGENTD_REMOTE_WORKERS_CONFIG` is consumed by the optional remote-worker
controller, not by `ServiceConfig`, so it is intentionally not part of the table
above. It names a strict JSON document with exactly `{"workers": [...]}` at the
top level. Each worker object requires `name`, `host`, `port`, `node_id`, and
`session_epoch`; unknown fields are rejected. Exactly one of `psk_env` and
`psk_file` must be provided. Environment PSKs require at least 32 bytes; file
PSKs must be regular, non-symlink files with mode `0600`. Inline PSKs or other
inline secret values are rejected. Resolved PSK values must be unique across
configured nodes; shared worker trust domains are rejected.

Configured endpoint `features` express requirements only. They do not grant
capabilities: the backend becomes eligible after an authenticated heartbeat
reports the required features for the expected worker driver.

The document is limited to 1 MiB and 128 workers; duplicate JSON keys and
non-finite values are rejected. TLS 1.2 or newer with hostname verification is
the default. `allow_insecure_loopback` is an explicit plaintext exception for
`127.0.0.1`, `::1`, or `localhost` only, and cannot be combined with TLS file
settings. The complete schema and endpoint-field rules are in the [operational
configuration guide](../40-operations/configuration.md#remote-worker-controller-configuration).
