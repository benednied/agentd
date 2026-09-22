# Native backlog operations

The native backlog reader observes GitHub's issue, sub-issue, and dependency
endpoints. It does not infer a plan from issue prose and it never authorizes
work from a partial page. An exact read is required before an approval grant.
Use a bounded read only for inspection when the operator accepts that the page
limit may omit nodes.

The repository's durable selection is either one epic or an explicit issue
set. The example fragments in [native-backlog-epic.json](../../deploy/examples/native-backlog-epic.json)
and [native-backlog-issues.json](../../deploy/examples/native-backlog-issues.json)
show both forms. Pull request numbers are administrative mappings and product
decisions remain explicit operator inputs. They are not taken from issue text.

The host paths come from `deploy/env/coding.env.example`: `coding-config`,
`coding-transport`, `coding-publication-cache`, and the controller state root.
The Compose services mount those paths at the in-container paths used by the
example JSON. Prepare the Linux host in this order:

For host-specific read-only mounts, place a reviewed Compose override at
`/home/bened/.local/share/agentd/coding.override.yaml`. The coding systemd
units and release script add this regular non-symlink file automatically when
it exists. Keep the generic `compose.coding.yaml` portable; use the override
for machine-specific runtime or dependency virtualenv paths.
The worker's base Compose service supplies private writable `/run/agentd` and
UV-cache tmpfs mounts required by its runtime preflight; host overrides should
preserve those mounts when extending the service.

1. Create the exact state directories as the service account:

   ```bash
   install -d -m 0700 /home/bened/.local/state/agentd/coding-controller
   install -d -m 0700 /home/bened/.local/state/agentd/coding-config
   install -d -m 0700 /home/bened/.local/state/agentd/coding-transport
   install -d -m 0700 /home/bened/.local/state/agentd/coding-publication-cache
   install -d -m 0700 /home/bened/.local/state/agentd/coding-worker
   install -d -m 0700 /home/bened/.local/share/agentd/workspaces/coding-worker
   install -d -m 0700 /home/bened/.local/share/agentd/coding-worker-codex-home
   ```

2. Initialize the trusted bare object cache from the reviewed repository and
   verify its ownership:

   ```bash
   git clone --bare https://github.com/OWNER/REPOSITORY.git \
     /home/bened/.local/state/agentd/coding-publication-cache/repo.git
   git --git-dir=/home/bened/.local/state/agentd/coding-publication-cache/repo.git fsck --full
   ```

3. Generate the worker transport secret and install the reviewed worker TLS
   certificate/key. The certificate authority file used by the controller must
   contain the issuing CA or the worker certificate when a dedicated CA is not
   used. Do not paste credentials into JSON or commit them:

   ```bash
   umask 077
   openssl rand -out /home/bened/.local/state/agentd/coding-transport/worker.psk 32
   chmod 600 /home/bened/.local/state/agentd/coding-transport/worker.psk
   # Install the qualification-approved coding-profiles.json beside worker.psk.
   install -m 0600 /absolute/reviewed/coding-profiles.json \
     /home/bened/.local/state/agentd/coding-worker/coding-profiles.json
   # Install reviewed worker.crt and worker.key into coding-transport/.
   chmod 600 /home/bened/.local/state/agentd/coding-transport/worker.key
   chmod 644 /home/bened/.local/state/agentd/coding-transport/worker.crt
   ```

4. Copy the example controller and publisher configuration, then replace the
   repository identity, numeric repository ID, base commit, and validation
   commands with reviewed values:

   ```bash
   install -m 0600 deploy/examples/coding-controller.json \
     /home/bened/.local/state/agentd/coding-config/controller.json
   install -m 0600 deploy/examples/coding-publisher.json \
     /home/bened/.local/state/agentd/coding-config/publisher.json
   ```

5. Add the selected `backlog` object from one of the native examples to both
   trusted controller documents. Preview the graph and record its revision
   before approving it. The approval actor and revision are durable evidence.

The operational sequence is: exact discover, inspect graph and integration
status, approve the exact revision, then allow the controller to poll. A graph
approval does not approve product decisions, existing pull requests, changed
issues, or validation results. `status` is the health view for durable jobs,
worker heartbeats, quota wait reasons, and publication outcomes.

Run the controller with publication disabled and the publisher separately. These
commands run inside the configured Compose services; host paths are translated
by the mounts:

```bash
/opt/agentd/venv/bin/agentd github --config /etc/agentd/controller.json serve --without-publication
/opt/agentd/venv/bin/agentd github --config /etc/agentd/controller.json publish
```

Preview and approve inside the configured controller container (its configuration
is mounted at `/etc/agentd/controller.json`):

```bash
agentd github --config /etc/agentd/controller.json plan
agentd github --config /etc/agentd/controller.json approve-graph --revision GRAPH_REVISION --actor operator --mode exact
```

For a native relationship import, mount the reviewed manifest read-only into an
administrative container at `/tmp/native-backlog.json` and supply a GitHub
credential authorized to modify the selected repository. The runtime controller's
read-only GitHub credential is insufficient for this administrative operation.
Use the import preview's own revision, which also binds the existing native
relationships, rather than the plan's graph revision:

```bash
agentd github --config /etc/agentd/controller.json import-graph /tmp/native-backlog.json
agentd github --config /etc/agentd/controller.json import-graph /tmp/native-backlog.json --apply --revision IMPORT_REVISION --actor operator
```

The first `import-graph` is preview only. `--apply` is the separate explicit
import decision. A bounded provider read is an observation bound; a bounded
approval grant is an administrative choice and never turns an incomplete graph
into an authorizable snapshot.

The controller owns scheduling and the single database lock. The publisher
owns only the publication ledger and GitHub publication credentials. Keep both
processes under the dedicated Linux systemd units and preserve their state
across restarts. A worker heartbeat failure blocks new admission and does not
erase retained runs or checkpoints.

The native backlog importer and runtime still require repository preparation.
The dependency qualification environment currently uses a Goldenage-specific
monkeypatch and has not yet been productized into a general deployment step.
There is no live migration or completed unattended qualification from this
document.

## Dedicated release activation

The generic `deploy.sh` and `rollback.sh` manage the legacy single-service
profile. For the coding profile, prepare a SHA-versioned image and release tree,
with `release.env` containing its `AGENTD_IMAGE`, and install the reviewed
`coding.env` at `/home/bened/.local/share/agentd/coding.env`. Then invoke:

```bash
./deploy/scripts/coding-release.sh activate /absolute/prepared-release
# To select a previously prepared compatible release:
./deploy/scripts/coding-release.sh rollback /absolute/previous-release
```

This script manages the separate `coding-current` symlink. It drains an active
coding controller, refuses unresolved execution ownership, and leaves the new
release drained. Startup success is not a readiness or qualification result;
inspect `health` and `status` before explicitly running `undrain`. Rollback
preserves current databases, quota accounting and checkpoints; only use a
release compatible with that state. Migration from the older laptop-controlled
Goldenage worker is a separate handoff and is not performed by this script.

The persistent worker accepts an explicit `--model` (default `gpt-5.6-luna`).
For a prebuilt Python environment, specify `--dependency-venv`,
`--dependency-profile-id`, and `--dependency-python-root`. Preparation applies
only to that profile, copies the environment into the fresh lease, relocates
entrypoints, and removes editable links to the preparation checkout. External
symlinks must resolve under the explicitly configured interpreter installation;
mount that installation read-only at the same path. No package download occurs
inside a coding run. The publisher independently needs its own read-only runtime
mounts and validation commands in its trusted profile. Previously published
ledger entries remain terminal when a new platform profile is installed.
