# Hardened single-host deployment

This deployment profile is for the reviewed single-user Linux host layout. It is
deliberately local: none of the scripts use SSH, copy files to another machine, or
contact a host named HP. Build and service commands run only where the operator
invokes them.

## Security boundary

The container runs as UID/GID `1000:1000`, with a read-only root filesystem, all
capabilities dropped, `no-new-privileges`, a 512 PID ceiling, 6 CPUs, and 12 GiB of
memory. There are no published ports, privileged mode, Docker socket mount, or
broad `/`, `/home`, `/home/bened`, or `/root` bind mounts.

The user-systemd wrapper also applies `NoNewPrivileges`, `PrivateTmp`, strict
system/home protection, address-family restriction, and personality/SUID controls.
It deliberately does not request `PrivateDevices`: the reviewed HP user manager
cannot apply that directive without capabilities. Device isolation remains at the
Compose boundary, which defines no device mappings, runs non-privileged, and drops
all capabilities.

Only these five writable bind mounts are permitted, with the same narrow paths on
the host and in the container:

| Purpose | Exact path |
| --- | --- |
| SQLite state and backups | `/home/bened/.local/state/agentd` |
| Git worktrees | `/home/bened/.local/share/agentd/workspaces` |
| Dedicated Codex home | `/home/bened/.local/share/agentd/codex-home` |
| Dedicated writable uv cache | `/home/bened/.cache/uv` |
| Goldenage repository | `/home/bened/goldenage` |

The normal home directory is not mounted. `/tmp` and `/run/agentd` are bounded
`noexec,nosuid,nodev` tmpfs mounts. The dedicated Codex home is not part of the
image and is not a general user configuration directory. It contains only the
explicit nonsecret service `config.toml`, the explicitly provisioned
`auth.json`, and state created later by this dedicated Codex runtime.

The seccomp profile is default-deny. It admits ordinary Python, Git, SQLite,
network, and process syscalls plus the namespace and mount operations needed by a
nested unprivileged Bubblewrap sandbox. The image includes `bubblewrap`, `uidmap`,
and the same pinned `uv` executable used at build time. `clone` and `unshare` are
mask-filtered to reject UTS, cgroup, and time namespace creation while admitting
the user, mount, IPC, PID, and network namespaces Bubblewrap needs; `setns` is
restricted to user namespaces. `clone3` cannot be flag-filtered by classic seccomp because its
arguments are behind a pointer, so it is the one explicit modern-kernel namespace
compatibility exception. Capability dropping and `no-new-privileges` remain in
force inside and outside a nested namespace.

The pinned `openai-codex==0.144.4` generated App Server schema does not retain
the newer restricted-read policy field. A same-UID model command must therefore
not rely on read-only outer mounts: those mounts would still allow it to read
`auth.json` and the SQLite database. The image puts a root-owned wrapper ahead
of Bubblewrap on `PATH`. Every nested sandbox invocation is delegated to
`/usr/bin/bwrap` only after the wrapper injects `--unshare-net` and final
`--tmpfs` masks over the exact dedicated Codex home and agentd state directory.
It rejects `--share-net` and invocations without an explicit command separator.

This wrapper is a fail-closed compatibility boundary, not an assertion that the
SDK policy is sufficient. Service startup executes both a direct nested
Bubblewrap probe and a command through the exact SDK-bundled Codex App Server.
Both probes must prove all of the following: the wrapper was invoked,
`auth.json` is unreadable, the SQLite database is unreadable, the temporary
leased worktree is writable, and an AF_INET route cannot be selected. Any
failure, including Codex bypassing the wrapper, aborts startup and therefore the
deployment.

## Prerequisites

- Linux with unprivileged user namespaces enabled and
  `user.max_user_namespaces > 0`.
- The host-provided `lxc-usernsexec` AppArmor profile loaded. On the reviewed HP,
  this is the existing unconfined execution profile with an explicit `userns`
  grant; Docker's default profile blocks nested mount propagation. Agentd still
  drops every container capability, enables `no-new-privileges`, and applies its
  own default-deny seccomp profile. Deployment fails closed if the named profile
  is absent or nested Bubblewrap cannot start.
- A local account whose UID and GID are both 1000 and whose home is
  `/home/bened`.
- Goldenage already checked out at `/home/bened/goldenage`, owned by UID/GID 1000.
- Docker Engine with Compose v2 and BuildKit. Rootless Docker is preferred. Access
  to a rootful Docker socket through the `docker` group is effectively host-root
  authority even though that socket is never mounted into the container.
- `git`, `python3` (including its bundled `sqlite3` module), `sha256sum`, `tar`,
  and user systemd.
- At least 6 available CPUs, 12 GiB RAM, and storage for immutable releases,
  images, worktrees, and SQLite backups.
- Build-time access to the pinned Python, Node, uv, Debian, and Codex package
  sources, or those images/packages preloaded in a controlled local registry or
  cache. Runtime startup uses `pull_policy: never`.

The image pins Python, Node, uv, and Codex versions by build argument. For a fully
reproducible supply chain, override the base-image arguments with reviewed digest
references and retain the resulting image digest with the release record.

## Trusted one-time provisioning

Review `deploy/scripts/provision-host.sh` locally before running it. The script may
run as root or directly as the exact UID 1000 service owner. It creates only paths
owned by that account and installs only that account's user unit.
It does not install packages, change sysctls, enable services, copy shell history,
copy Codex configuration, or contact another host.

Provisioning requires an explicitly selected, reviewed `auth.json` source. It
validates that the source is a small, non-symlink JSON object, backs up an existing
target, and installs only that `auth.json` from the supplied source with mode
`0600` into the `0700` dedicated Codex home. Separately, it installs the reviewed,
nonsecret `deploy/container/config.toml` with mode `0600`; it never imports a
general Codex configuration, shell history, or session history. The auth source
is never copied into the repository, release, Docker build context, image,
environment file, or command line:

```bash
export AGENTD_AUTH_SOURCE=/secure/reviewed/auth.json
sudo --preserve-env=AGENTD_AUTH_SOURCE ./deploy/scripts/provision-host.sh
```

When already logged in as UID 1000, run it directly instead:

```bash
AGENTD_AUTH_SOURCE=/home/bened/.codex/auth.json \
  ./deploy/scripts/provision-host.sh
```

Follow the host's reviewed `sudo` environment policy. Do not use a source inside
the repository or a shell-history/config directory. Re-run the reviewed
provisioning step when a release intentionally changes the pinned service
`config.toml`; the runtime check rejects a host copy that differs from the image.

Then, as UID 1000:

```bash
systemctl --user daemon-reload
systemctl --user enable agentd.service
```

If the service must survive logout, a host administrator may enable lingering for
the dedicated user after reviewing that operational choice.

## SHA-versioned deployment

Deployment accepts only a full lowercase 40-character Git commit SHA. It exports
that committed tree into a staging directory, builds `agentd:<SHA>` locally with an
OCI revision label, renders and validates Compose, then installs the immutable
release under:

```text
/home/bened/.local/share/agentd/releases/<SHA>/
```

Before changing the atomic `current` symlink, it stops an active service and takes
a SQLite online backup with an integrity check and checksum under:

```text
/home/bened/.local/state/agentd/backups/<timestamp>-before-<SHA>-<pid>/
```

Deploy from the reviewed agentd checkout. Goldenage remains a separately mounted
test repository. The optional source and image-repository arguments are shown
explicitly here:

```bash
./deploy/scripts/deploy.sh \
  0123456789abcdef0123456789abcdef01234567 \
  /home/bened/.local/share/agentd/source \
  agentd
```

The generated `release.env` contains only image, path, timing, model, and quota
policy values. Git author and committer identity are fixed inside Compose to
`agentd automation <agentd@localhost>` so disposable worker-branch commits do not
write general Git configuration into the dedicated Codex home.

Releases and backups are never pruned automatically. Retention is a separate,
reviewed operator action.

## Rollback

Rollback is explicit: choose an installed target SHA and a backup whose
`release.sha` manifest matches that target. The script first creates another
safety backup of the current state, stops the service, validates the selected
backup checksum and SQLite integrity, restores it atomically, switches `current`,
and starts the target release:

```bash
./deploy/scripts/rollback.sh \
  0123456789abcdef0123456789abcdef01234567 \
  20260809T180000Z-before-fedcba9876543210fedcba9876543210fedcba98-1234
```

If target startup fails, the script restores the pre-rollback state and prior
release when available. Rollback never deletes a release or backup. Repository and
worktree data are intentionally not overwritten by a state rollback.

## Verification

Static tests do not require a Docker daemon:

```bash
uv run pytest tests/deployment -q
uv run ruff check .
uv run ruff format --check .
```

After building a SHA image and generating `release.env`, validate rendered Compose
without starting a container:

```bash
./deploy/scripts/check-container-security.sh \
  /home/bened/.local/share/agentd/current/release.env static
```

The runtime check starts only the local image with a shell override. It invokes
the pinned local Codex App Server binary but starts no model turn, sends no prompt,
and calls no LLM API. It verifies UID/GID, zero effective capabilities,
`NoNewPrivs`, read-only root, absence of Docker sockets and a broad home mount,
and successful unprivileged user-namespace creation. It then runs the direct and
Codex-generated-command sandbox probes described above. The network assertion is
a UDP route selection to the documentation-only `192.0.2.1` address and sends no
packet:

```bash
./deploy/scripts/check-container-security.sh \
  /home/bened/.local/share/agentd/current/release.env runtime
```

The user systemd unit runs this check as a mandatory `ExecStartPre`; it is not an
optional warning in normal deployment. A missing auth/config/state file, wrong
ownership or mode, unavailable nested user namespace, wrapper bypass, readable
sensitive file, unwritable lease, or available network route prevents the
container service from starting.
