# Security boundary

The root [security policy](../../SECURITY.md) defines reporting and supported
revisions. This document describes the reviewed runtime boundary.

The container runs as UID/GID `1000:1000`, with a read-only root filesystem, all
capabilities dropped, `no-new-privileges`, a 512 PID ceiling, 6 CPUs, and 12 GiB
of memory. It publishes no ports, uses no privileged mode or Docker socket, and
does not mount broad `/`, `/home`, `/home/bened`, or `/root` paths.

The user-systemd wrapper applies `NoNewPrivileges`, `PrivateTmp`, strict system and
home protection, address-family restriction, and personality/SUID controls.

Only these narrow writable bind mounts are allowed:

| Purpose | Path |
| --- | --- |
| SQLite state and backups | `/home/bened/.local/state/agentd` |
| Git worktrees | `/home/bened/.local/share/agentd/workspaces` |
| Dedicated Codex home | `/home/bened/.local/share/agentd/codex-home` |
| Writable uv cache | `/home/bened/.cache/uv` |
| Reviewed repository | `/home/bened/goldenage` |

`/tmp` and `/run/agentd` are bounded `noexec,nosuid,nodev` tmpfs mounts. The
dedicated Codex home contains only the explicit service configuration, provisioned
authentication, and state created by this Codex runtime.

The default-deny seccomp profile admits ordinary Python, Git, SQLite, process,
network, namespace, and mount operations required by nested unprivileged
Bubblewrap. It filters namespace flags and restricts `setns`; `clone3` is the
documented compatibility exception because classic seccomp cannot inspect its
pointer arguments.

The root-owned `/usr/bin/bwrap` compatibility shim rewrites only the exact reviewed
`--dev /dev` invocation and exact App Server helper path to private root-owned
aliases. It rejects alternate device targets, argument expansion, shared network,
unknown options, and malformed invocations. Startup audits both direct and
App-Server-generated sandbox probes and fails closed if an invariant is missing.

Codex uses the named `agentd-workspace` permission profile. Platform and toolchain
paths are read-only; the current lease is the only writable runtime root; Codex
home, SQLite state, the workspace-pool parent, and command network access remain
denied. The dependency provisioner is the only boundary with deliberate network
access.

An isolated deployment may pass an explicit JSON mount policy to the validator.
The policy must enumerate the reviewed container targets and narrow absolute host
sources. Broad mounts, missing targets, implicit host-path creation, or readable
sensitive files remain rejected.
