# Reviewed self host releases

The trusted HP release supervisor upgrades agentd after an approved GitHub PR
reaches `master`. It runs outside coding workers and owns Docker and systemd
operations. Workers receive no Docker socket or release credential. The
supervisor checks every new first parent commit, prepares an immutable SHA
release, proves containment, independently validates package artifacts, takes
consistent SQLite backups, and invokes the drained coding activation procedure.

The baseline is `5d3aba3212957b8628a736f12f22eeb854357d9a`. Existing master is
skipped while the bootstrap release runs. New master must include the standing
GitHub workflow, allowance and health modules, trusted validator, and self host
service units. A merged commit missing those components cannot replace the
bootstrap release.

## Package validation

The image prepares `/opt/agentd/validation-venv` from frozen development and
runtime dependencies without installing the source project. It includes Python
3.12 or newer, pytest, Ruff, build, hatchling, packaging, and pip 22.3 or newer.
The trusted validator lives at `/opt/agentd/tools/validate_python_package.py` and
is read only inside the publication sandbox. It checks the exact clean Git
checkout, runs lint, formatting, documentation and tests against `checkout/src`,
builds wheel and source distribution offline, and installs the wheel into a
fresh environment. The smoke test checks wheel import location, version,
declared dependency compatibility, and the installed CLI. Artifact digests and
command results are retained in qualification evidence. A final clean tracked
diff check rejects source mutations made during tests, builds or smoke checks.

There is no nested `uv run` in validation. Newly declared dependencies must be
prepared in a trusted runtime before candidate validation; the sandbox cannot
download them. A mismatch fails validation rather than testing an old installed
agentd package or silently accepting incompatible dependencies.

## Supervisor configuration

Save mode 0600 configuration at
`/home/bened/.local/state/agentd-selfhost/coding-config/release-watcher.json`.
The two protected GitHub executables can be Docker wrappers. The review wrapper
needs repository read access; the status wrapper additionally needs permission
to create and edit its own comments on the configured operations issue. Their
credentials must remain outside model mounts. When bootstrap integration is
configured, the status wrapper also needs permission to mark that PR ready and
merge it.

```json
{
  "source_repository": "/home/bened/agentd",
  "repository_name": "benednied/agentd",
  "release_root": "/home/bened/.local/share/agentd-selfhost/releases",
  "status_file": "/home/bened/.local/state/agentd-selfhost/release-status.json",
  "baseline_commit": "5d3aba3212957b8628a736f12f22eeb854357d9a",
  "approved_actors": ["benednied"],
  "approved_actor_ids": {"benednied": 116355829},
  "gh_config_dir": "/home/bened/.local/state/agentd-selfhost/coding-github-read",
  "gh_executable": "/home/bened/.local/share/agentd-selfhost/gh-release-read",
  "status_gh_executable": "/home/bened/.local/share/agentd-selfhost/gh-release-status",
  "status_issue_number": 86,
  "databases": [
    "/home/bened/.local/state/agentd-selfhost/coding-controller/state.sqlite"
  ],
  "image_repository": "agentd-selfhost",
  "poll_seconds": 300
}
```

Replace the example issue number with the bootstrap operations issue, and list
every live controller, SDK and operation journal database in `databases`.
The source origin must be exactly `https://github.com/benednied/agentd.git`.
A release requires either an explicit merge by an approved actor or that actor's
latest approval of the exact PR head. Direct pushes and rewritten history stop
activation. Configure the actor ID mapping so approvals require both login and
immutable identity, and bind the PR base to repository ID `1328873039`.

The optional trusted `bootstrap_pr` configuration contains `number`,
`head_commit`, `activated_at` in UTC, and `required_checks` such as `["quality"]`.
Use the bootstrap PR number and the full SHA of the qualified deployed release.
This one PR can be approved through GitHub even when the publication account
cannot review its own PR: post `/agentd approve` or `/agentd approve <full SHA>`
as a new, unedited PR issue comment after the configured activation time. Edited
commands are ignored; post a new comment instead. A native approval submitted
after activation for that exact head also qualifies. Both paths require the configured
trusted login and immutable numeric user ID. PR descriptions, quoted commands,
machine marked comments, older comments, and other heads do not authorize it.
An unresolved latest changes request from any trusted reviewer vetoes merging.

Before making a draft ready or merging, the supervisor verifies the PR's exact
head and immutable base repository, latest successful GitHub Actions result for
each required job, qualification evidence, image source label and digest, and
all three running service images. It reads approval and checks again before the
merge and passes the expected head SHA to GitHub's atomic merge gate. Lost ready
or merge responses reconcile through a fresh PR read. Pending approval or checks
appear on the operations issue. Omitting `bootstrap_pr` preserves ordinary
reviewed master observation.

Install `deploy/systemd/agentd-selfhost-release.service` in the user's systemd
directory and enable it after the coding release is healthy. The HP requires
Python 3.12 or newer, Git, Docker with the reviewed user namespace policy, and an
active lingering user service bus for UID 1000. The service reads configuration
at startup, writes its durable status and qualification files, and runs every
five minutes by default.

The operations issue receives one machine identified comment with meaningful
stage changes or blockers. Repeated unchanged observations stay quiet. Lost
comment responses reconcile the same authenticated comment before retrying.
The comment contains no credentials or raw host command output.

The host supervisor also reads controller and publisher polling pulses, source
freshness, provider telemetry, and authenticated worker heartbeats independently
of the publisher. Two consecutive failed health observations appear on the
operations issue by default, even if publication itself is down. Quota pressure,
draining, dependency waits, and a normal active job are distinct from stale
polling or unresolved provider ownership. This monitor reports failures without
killing an active worker. After successful activation it records the deployed
SHA and queues its own restart to load the reviewed supervisor implementation.

## Activation and recovery

`coding-release.sh` requires a live controller, a drain, and fresh authenticated
idle worker evidence before stopping existing services. Automatic activation
restores admission only after the new controller, source and worker gates pass.
The supervisor retains databases and backups across failures and never restores
an old database over newer state. If a new migration prevents binary rollback,
keep the new release drained and repair forward rather than reverting its ledger.

Package gates, Docker builds and host restart behavior still require real HP
qualification. The supervisor's unit tests establish policy and ordering;
they do not establish successful deployment or a completed unattended soak.
