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

## Codex quota and reauthorization

`codex-status` exits with code `2` and reports `reauthorization_required` when
the service session is missing, unusable, or rejected during refresh. The main
daemon logs `provider_reauthorization_required`. The coding controller exposes
the same condition in `github health` under `provider_auth`, alongside process
`liveness`. A failed poll leaves the last successful snapshot in place and
unattended admission still enforces the existing freshness limit.

On the credential owner's host, run these commands as the service user. Use the
service's dedicated home and the account and workspace assigned to that service.

```bash
uv run agentd --codex-home /absolute/path/to/service-codex-home \
  codex-reauthorize --device-auth
uv run agentd --codex-home /absolute/path/to/service-codex-home \
  --db /absolute/path/to/controller.sqlite codex-status --pool codex
```

Use the browser flow by omitting `--device-auth`. The command signs in to a
private staging directory and atomically installs the new session. The next
successful poll clears the controller's auth status. For a worker-only home,
verify the worker's preflight and authenticated heartbeat after sign-in.

For deployments that previously copied a Codex cache, stop the services using
the copied session during migration. Run a separate sign-in for every daemon,
controller, and worker home that used the copy. Use a fresh sign-in for each
new home. Keep the lock file and credential directory on the owner's local
filesystem and use `codex-reauthorize` for future sign-ins to those homes.

## GitHub REST budget

Run `agentd github --config /path/to/controller.json health` to inspect
`github_rest` and `source_observed_at`. The REST report includes cumulative
request counts by component, operation, and outcome, plus the last reported
remaining budget and reset time. A `304` is a conditional response confirming
that the representation is unchanged. GitHub excludes authenticated `304`
responses from the primary quota.

The controller and publisher share one REST budget per controller database.
On a primary or secondary limit, both pause REST requests until `retry_at`.
The deadline survives restarts. `reset_at`, `retry_at`, and the quota's
`observed_at` use Unix seconds. The existing source poll cadence and
three-minute freshness window still apply. A failed or deferred sweep leaves
`source_observed_at` at the last complete observation.

The offline replay models one quiet hour at the HP's 60-second cadence, with
28 tracked issues, 26 published PRs, and 25 publisher PR candidates. The old
request estimates are from [issue #125](https://github.com/benednied/agentd/issues/125).
The replay returns stable ETags and includes a publisher review read for each
candidate on every sweep.

| Component | Previous primary requests, estimated | Replay primary requests | Replay `304` responses |
| --- | ---: | ---: | ---: |
| Controller | 5,220 | 31 | 1,829 |
| Publisher | 3,000 before review reads | 51 | 3,009 |

The replay made 4,920 HTTP requests and used 82 of the 5,000 primary requests.
Compared with the previous estimated demand of 8,220 requests per hour, this
is about 40% fewer HTTP requests and 99% fewer primary requests. Live usage
depends on changed responses, extra pages, cold starts, and other clients
sharing the account.

## Metering and worktrees

- `METERING_PENDING` means terminal usage was absent, inconsistent, or not present
  in the durable ledger. Capacity is released, but acceptance is blocked.
- A dirty worktree is retained for inspection. Normal cleanup never forces removal
  of uncommitted worker output.
- `serve` recovers durable managed runs, polls account telemetry, reconciles
  commands, dispatches ready work, and stops on `SIGINT` or `SIGTERM`.

For deployment-specific failures, inspect the [security verification](security.md)
and [deployment](deployment.md) requirements.
