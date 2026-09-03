# Operational configuration

Configuration has three separate concerns:

1. `ServiceConfig` defines portable paths and policy values for the control plane.
2. `WorkerServeConfig` defines the separately authenticated worker listener,
   durable journal, TLS/PSK inputs, and typed-operation cache/allowlist roots.
3. The reviewed deployment composition supplies exact host paths, credentials,
   mounts, and production policy at its trust boundary.

The complete list of supported variables, defaults, and validation rules lives in
the normative [configuration reference](../80-reference/configuration.md). This
page explains how operators use those values; it intentionally does not repeat the
reference table.

## Local development

For the fake path, use a project-local database and a workspace root outside the
repository checkout. CLI path flags override the corresponding environment values:

```bash
uv run agentd \
  --db /absolute/path/to/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  init
```

The fake path does not require a Codex home or authenticated provider account.

## Remote artifact worker

Start `worker-serve` only with a node identity, durable operation journal, PSK,
and the default TLS configuration (or an explicitly loopback-only plaintext
exception). Its operations document allowlists Git repositories, registries, and
Compose targets. The worker executes typed Build/Deploy contracts; it is not a
general remote shell or coding-agent host. See the [CLI reference](../80-reference/cli.md)
and [worker-backend architecture](../30-architecture/worker-backends.md).

Worker-specific environment variables are intentionally separate from the
control-plane configuration reference:

| Variable | Meaning |
| --- | --- |
| `AGENTD_REMOTE_WORKERS_CONFIG` | controller-side strict JSON endpoint document |
| `AGENTD_WORKER_HOST`, `AGENTD_WORKER_PORT` | listener address and port |
| `AGENTD_WORKER_NODE_ID`, `AGENTD_WORKER_SESSION_EPOCH` | node identity and durable epoch |
| `AGENTD_WORKER_JOURNAL` | SQLite operation journal |
| `AGENTD_WORKER_PSK_ENV`, `AGENTD_WORKER_PSK_FILE` | mutually exclusive PSK sources |
| `AGENTD_WORKER_TLS_CERT`, `AGENTD_WORKER_TLS_KEY` | paired TLS certificate/key |
| `AGENTD_WORKER_ALLOW_INSECURE_LOOPBACK` | explicit loopback-only plaintext exception |
| `AGENTD_WORKER_OPERATIONS_CONFIG` | strict typed-operation allowlist |
| `AGENTD_WORKER_CACHE_ROOT`, `AGENTD_WORKER_OPERATION_STATE_ROOT` | operation cache and deployment state roots |

The PSK must contain at least 32 bytes. It comes from the named environment
variable or from a regular, non-symlink mode-`0600` file, never both. Worker TLS
uses minimum TLS 1.2 and requires the certificate/key pair unless the operator
explicitly enables plaintext on a loopback host. The operations configuration
contains exactly `repositories`, `registries`, and `compose_targets`; repository
entries reject SSH/scp transports. Git and Docker commands receive only reviewed
runtime variables (for example PATH, Docker/TLS/proxy configuration, and locale),
not the daemon's complete environment; system/global Git configuration is disabled
for worker Git commands. Each Compose target contains an allowlisted
`source_repository`, a confined repository-relative `compose_file`, and explicit
trusted `environment` values. The worker materializes the requested full config
commit in a detached cache worktree. Operation-owned image, config revision, and
deployment-name values take precedence. A target's resolved Compose model must
use allowlisted digest-pinned images and mark the requested image service with the
exact config revision through `AGENTD_CONFIG_REVISION` or the
`agentd.config-revision` label.

## Remote worker controller configuration

`AGENTD_REMOTE_WORKERS_CONFIG` points to the controller's strict JSON worker
configuration. The document must contain exactly one top-level field,
`workers`, whose value is a list of at most 128 endpoint objects. The file is
limited to 1 MiB; duplicate object keys and non-finite JSON values are rejected.
Unknown endpoint fields are rejected. Each endpoint requires `name`, `host`,
`port`, `node_id`, and `session_epoch`; its optional fields are `psk_env`,
`psk_file`, `tls_ca`, `tls_client_cert`, `tls_client_key`, `server_hostname`,
`allow_insecure_loopback`, `operating_system`, `architecture`, and `features`.
Paths are resolved relative to the configuration file.

`psk_env` and `psk_file` are mutually exclusive and exactly one is required.
The environment value must contain at least 32 bytes; a file must be regular,
non-symlink, and mode `0600`. Secrets may not appear inline in JSON. TLS is the
default: the controller uses TLS 1.2 or newer with hostname verification. Every
configured node must resolve to a distinct PSK value; sharing a key across nodes
is rejected so one worker cannot authenticate as another. A CA file and client
certificate/key are optional, but the certificate and key must be paired.
`allow_insecure_loopback` is an explicit plaintext exception and is valid only
for `127.0.0.1`, `::1`, or `localhost`; it cannot be combined with TLS file
settings.

Endpoint `features` are fail-closed requirements, not controller-side capability
claims. A remote backend begins unhealthy and without Build/Deploy verification
features; only the authenticated worker heartbeat can bind the feature set of its
registered `operations` driver. Dispatch remains blocked if any configured
requirement is absent.

For example, this is the shape (the PSK itself is deliberately absent):

```json
{
  "workers": [
    {
      "name": "build-node-1",
      "host": "worker.example.test",
      "port": 8765,
      "node_id": "build-node-1",
      "session_epoch": "2026-08-31T12:00Z",
      "psk_env": "AGENTD_BUILD_WORKER_PSK",
      "tls_ca": "/etc/agentd/worker-ca.pem",
      "features": ["build-image", "deploy-image"]
    }
  ]
}
```

This controller document configures node-bound authenticated artifact workers; it
does not configure SSH/SCP transport, a Kubernetes or HA fleet, or a web UI.

## Reviewed service deployment

The deployment profile uses dedicated state, workspaces, Codex home, uv cache, and
repository paths. The host-specific values are defined in `deploy/env/` and
validated by the Compose security checks; they are not portable `src/agentd`
defaults.

Trusted `serve` composition enforces the reviewed production model and reasoning
policy. The portable configuration type can still represent non-production values
for tests and local integrations. See [deployment](deployment.md) and
[security](security.md) for the operational boundary.

The daemon applies fresh provider telemetry to admission and reconciliation. The
provider-stop policy has a fixed hard stop at 2% remaining reported quota; values
that would loosen or tighten that boundary are rejected. Stale telemetry cannot
prove the boundary. Reset detection records a durable reset event
only when a fresh, matching provider window and material used-fraction drop provide
evidence. Restart baselines are selected from the exact provider, pool, and bucket,
so an unrelated newer pool cannot hide a reset. An absolute post-reset amount is
required before quota is rewritten.

## Configuration changes

When adding or renaming a supported variable, update the source, the normative
reference, and the documentation check in the same change. Keep literal defaults in
the reference page rather than copying them into operational guides.
