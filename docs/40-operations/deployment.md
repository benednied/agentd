# Hardened single-host deployment

This profile is for the reviewed single-user Linux host layout. Commands run only
where the operator invokes them; scripts do not use SSH or copy files to another
host. `deploy.sh` remains the legacy single-host release/rollback path; it does
not provision or deploy a remote-worker fleet.

## Prerequisites

- Linux with unprivileged user namespaces and `user.max_user_namespaces > 0`;
- the host-provided `lxc-usernsexec` AppArmor profile with the reviewed `userns`
  grant;
- a local UID/GID `1000:1000` account with home `/home/bened`;
- Goldenage checked out at `/home/bened/goldenage`, owned by that account;
- Docker Engine with Compose v2 and BuildKit; rootless Docker is preferred;
- `git`, `python3` with SQLite, `sha256sum`, `tar`, and user systemd;
- at least 6 CPUs, 12 GiB RAM, and storage for releases, worktrees, images, and
  SQLite backups; and
- build-time access to the pinned Python, Node, uv, Debian, and Codex sources or
  controlled local caches.

The image pins Python, Node, uv, and Codex by build argument. Reviewed digest
references should be retained with the release record for a reproducible supply
chain.

## Provisioning

Review `deploy/scripts/provision-host.sh` before running it. Provisioning creates
only paths owned by the service account and installs its user unit. It does not
install packages, change sysctls, copy shell history or general Codex configuration,
or contact another host.

Provisioning requires an explicitly selected `auth.json` source. It validates a
small non-symlink JSON object, installs it as mode `0600` in the mode-`0700`
dedicated Codex home, and never copies the source into the repository, image,
environment file, or command line.

```bash
export AGENTD_AUTH_SOURCE=/secure/reviewed/auth.json
sudo --preserve-env=AGENTD_AUTH_SOURCE ./deploy/scripts/provision-host.sh
systemctl --user daemon-reload
systemctl --user enable agentd.service
```

The committed `deploy/container/config.toml` is installed atomically and later
deployment owns policy changes so the release-coupled copy can be backed up.

## SHA-versioned deployment

Deployment accepts a full lowercase 40-character Git SHA, exports that tree to a
staging directory, builds `agentd:<SHA>`, validates Compose, and installs the
immutable release under:

```text
/home/bened/.local/share/agentd/releases/<SHA>/
```

Before switching the atomic `current` symlink, it stops the service and backs up
SQLite plus the release-coupled Codex policy under:

```text
/home/bened/.local/state/agentd/backups/<timestamp>-before-<SHA>-<pid>/
```

Activation restores the previous release, database, and policy if installation or
startup fails. Releases and backups are never pruned automatically.

```bash
./deploy/scripts/deploy.sh \
  0123456789abcdef0123456789abcdef01234567 \
  /home/bened/.local/share/agentd/source \
  agentd
```

## Rollback

Rollback requires an installed target SHA and a backup whose `release.sha` matches
that target. It creates a safety backup, validates checksums and SQLite integrity,
restores via temporary files and atomic replacements, switches `current`, and
starts the target release. It never deletes releases or backups and does not
overwrite repository or worktree data.

```bash
./deploy/scripts/rollback.sh \
  0123456789abcdef0123456789abcdef01234567 \
  20260809T180000Z-before-fedcba9876543210fedcba98-1234
```

## Verification

Static checks:

```bash
uv run pytest tests/deployment -q
uv run ruff check .
uv run ruff format --check .
./deploy/scripts/check-container-security.sh \
  /home/bened/.local/share/agentd/current/release.env static
```

The runtime check starts only the local image, sends no model prompt, and calls
no LLM API. It verifies identity, capabilities, `NoNewPrivs`, read-only root,
mounts, helper ownership, nested user namespaces, sandbox probes, and the absence
of an available network route. The systemd unit runs this check as mandatory
`ExecStartPre`.

See [security](security.md) for the exact container boundary and [observability](observability.md)
for log handling.

## Self-hosted development on the HP

The `selfhost` profile runs agentd against its own repository in a separate
`agentd-selfhost` Compose project and `agentd-selfhost.service`. Its database,
worktrees, releases, Codex home, and uv cache are separate from the Goldenage
service. The HP must already satisfy the prerequisites above, with a complete
agentd Git checkout at `/home/bened/agentd`, owned by UID/GID `1000:1000`.

Run these commands on the HP, as `bened`, from that checkout. Select an existing
authenticated Codex `auth.json` explicitly; provisioning copies only that file
into the dedicated service home with mode `0600`.

```bash
AGENTD_PROFILE=selfhost \
  AGENTD_AUTH_SOURCE=/home/bened/.local/share/agentd/codex-home/auth.json \
  ./deploy/scripts/provision-host.sh
systemctl --user daemon-reload
systemctl --user enable agentd-selfhost.service
AGENTD_PROFILE=selfhost ./deploy/scripts/deploy.sh "$(git rev-parse HEAD)"
```

The exact alternate bind sources are recorded in
`deploy/security/selfhost-mount-policy.json`. The container retains the reviewed
internal paths: `/home/bened/goldenage` maps to the host's `/home/bened/agentd`,
and the internal state, workspace, Codex, and cache paths map to the isolated
self-hosting directories. No host service socket or deployment directory is
available to coding jobs. The mandatory runtime sandbox checks still run.

Register bounded capacity and token quota before submitting work:

```bash
docker exec agentd-selfhost-agentd-1 agentd register-quota codex \
  --provider openai-codex-chatgpt --remaining 500000 \
  --interactive-reserve 50000 --unit tokens
docker exec agentd-selfhost-agentd-1 agentd register-node hp-selfhost \
  --cpu 6 --ram-gb 12 --harness codex
docker exec agentd-selfhost-agentd-1 agentd submit \
  --project agentd --repository /home/bened/goldenage \
  --objective "Review and improve the self-hosting operator documentation; run validation" \
  --p50 60000 --p90 150000 --p99 300000 \
  --quota 150000 --quota-maximum 350000 --quota-pool codex \
  --harness codex --model-class gpt-5.6-terra \
  --accept "uv run ruff check . and uv run pytest tests/deployment -q pass"
docker exec agentd-selfhost-agentd-1 agentd jobs
```

These token amounts are local scheduling budgets, not a measurement of remaining
provider capacity. Metering includes cumulative input and output tokens,
including cached input across tool calls. Even a short documentation job can
exceed a small budget; the daemon requests a checkpoint at 90% of the maximum.
Size budgets from the usage ledger and keep the provider quota gate enabled.
`register-quota --remaining` initializes a new pool; registering an existing
pool preserves its live counters. It does not replenish consumed quota.

Dependency preparation selects the repository's `dev` dependency group or its
`dev` optional extra and honors a numeric `.python-version` pin before starting
Codex. Model commands use the prepared environment with synchronization disabled,
offline mode, and a worktree-local uv cache; the shared interpreter cache stays
read-only. Coding runs write only their leased
worktree and finish at the existing review gate. Operators inspect the handoff
commit and explicitly accept changes; deploying a new release remains an
operator action. Subsequent releases and rollbacks use the same scripts with
`AGENTD_PROFILE=selfhost`. If the HP is intended to run after logout, verify
`loginctl show-user bened -p Linger` reports `Linger=yes`.
