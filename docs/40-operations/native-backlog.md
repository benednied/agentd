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

Prepare the Linux host and configuration paths in this order:

1. Create the exact state directories as the service account:

   ```bash
   install -d -m 0700 /home/bened/.local/state/agentd/controller
   install -d -m 0700 /home/bened/.local/state/agentd/publication-cache
   install -d -m 0700 /home/bened/.local/state/agentd/transport
   ```

2. Initialize the trusted bare object cache from the reviewed repository and
   verify its ownership:

   ```bash
   git clone --bare https://github.com/OWNER/REPOSITORY.git \
     /home/bened/.local/state/agentd/publication-cache/repo.git
   git --git-dir=/home/bened/.local/state/agentd/publication-cache/repo.git fsck --full
   ```

3. Generate the worker transport secret and install the reviewed worker TLS
   certificate/key. The certificate authority file used by the controller must
   contain the issuing CA or the worker certificate when a dedicated CA is not
   used. Do not paste credentials into JSON or commit them:

   ```bash
   umask 077
   openssl rand -out /home/bened/.local/state/agentd/transport/worker.psk 32
   chmod 600 /home/bened/.local/state/agentd/transport/worker.psk
   # Install the reviewed worker.crt and worker.key into transport/.
   chmod 600 /home/bened/.local/state/agentd/transport/worker.key
   chmod 644 /home/bened/.local/state/agentd/transport/worker.crt
   ```

4. Copy the example controller and publisher configuration, then replace the
   repository identity, numeric repository ID, base commit, and validation
   commands with reviewed values:

   ```bash
   install -m 0600 deploy/examples/coding-controller.json \
     /home/bened/.local/state/agentd/controller.json
   install -m 0600 deploy/examples/coding-publisher.json \
     /home/bened/.local/state/agentd/publisher.json
   ```

5. Add the selected `backlog` object from one of the native examples to both
   trusted controller documents. Preview the graph and record its revision
   before approving it. The approval actor and revision are durable evidence.

The operational sequence is: exact discover, inspect graph and integration
status, approve the exact revision, then allow the controller to poll. A graph
approval does not approve product decisions, existing pull requests, changed
issues, or validation results. `status` is the health view for durable jobs,
worker heartbeats, quota wait reasons, and publication outcomes.

Run the controller with publication disabled and the publisher separately:

```bash
agentd github --config /home/bened/.local/state/agentd/controller.json serve --without-publication
agentd github --config /home/bened/.local/state/agentd/publisher.json publish
```

The controller owns scheduling and the single database lock. The publisher
owns only the publication ledger and GitHub publication credentials. Keep both
processes under the dedicated Linux systemd units and preserve their state
across restarts. A worker heartbeat failure blocks new admission and does not
erase retained runs or checkpoints.
