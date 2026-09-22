# Authorized GitHub coding

## Checkpointing and continuation

Remote coding now treats an interrupt/checkpoint separately from explicit
cancellation. After terminal provider ownership and valid usage are established,
the worker captures the retained edits with the trusted Git handoff owner and
writes an immutable checkpoint sidecar. The controller records `SUSPENDED`,
releases execution capacity, and retains the job, run history and charged usage.
Checkpoint capture can be finished after restart without another model call.

Continuation starts a **new run attempt and model session** on the same worker,
from the verified checkpoint commit. It is not a reattachment to a dead process.
The original issue, approval revision, repository, profile, base commit and
account must still match. The original failed/cancelled result remains intact.
The final draft includes the accumulated edits against the original PR base.
The worker SDK stores a separate execution envelope for each attempt, retaining
the logical job identity in its typed work order. Each envelope has its own
reservation; previous worker rows and usage samples remain unchanged.

`serve` automatically queues suspended checkpoints when fresh provider telemetry
allows admission and source approval remains valid. Set
`auto_resume_checkpoints: false` to require an operator. A provider reset does
not refill a job's cumulative token budget. Jobs at their 90% checkpoint threshold
remain suspended until their budget is explicitly replanned:

```sh
agentd github --config controller.json resume JOB_ID --actor operator
agentd github --config controller.json resume JOB_ID --actor operator --maximum-tokens 2000000
```

The new maximum is cumulative across attempts, not an additional allowance.
Remaining quota reservations, provider admission and worker limits still apply.
Unknown ownership, invalid telemetry, changed source and tampered checkpoint
bundles fail closed. No automatic merge or duplicate publication is introduced.

For legacy cancelled/failed runs, first quiesce the dedicated worker and use
`tools/qualify_coding_worker.py` with its existing protected deployment arguments
plus `--capture-run RUN_ID`. This administrative mode performs the containment
preflight and captures a proven terminal workspace without starting a model.
Save the returned checkpoint descriptor in an operator-controlled JSON file:

```sh
agentd github --config controller.json recover JOB_ID --actor operator --checkpoint checkpoint.json
agentd github --config controller.json resume JOB_ID --actor operator
```

`recover` only restores a suspended checkpoint; it does not execute anything or
erase the original terminal result/accounting. The descriptor is trusted operator
input from the worker capture command, never issue-supplied text. Retain worker
state and bundles until all continuation/review work is resolved.

Effort estimates default to half/the full profile runtime in minutes. Override
`effort_p50_minutes` and `effort_p90_minutes` for the workload; the old fixed
three/five-minute estimates were unsuitable for longer coding jobs. Both
`background_block_used_percent` and `urgent_only_used_percent` are explicit
operator policy (defaults 75 and 90, with the former strictly lower). Neither
setting bypasses fresh observations or provider exhaustion checks.

The bounded implementation composes the existing daemon, scheduler, quota
accounting, authenticated worker protocol, and review handoff. It has no public
control API. Enable it only inside one trusted administrative domain with a
contained coding worker. Generated pull requests are drafts; merge requires a
separate human decision.

## Configure authority

Configure an `IntakePolicy` with the canonical repository name, its immutable
GitHub numeric repository ID, and the eligibility label (`agentd:approved` by
default). Configure a `RepositoryProfile` with its HTTPS clone URL, allowed
harness, capability requirements, trusted validation commands, and runtime limit.
Pin a full base commit when compiling work orders. Profiles and approvals are
administrative configuration, never fields interpreted from issue instructions.

Use `GitHubIntake.approve(repository, number, actor=...)` from the trusted
controller to approve the currently observed title/body fingerprint. A label
alone is insufficient. Repeated polling resolves to the same durable job. Edits
revoke approval; closing an issue or removing eligibility cancels queued work and
requests cancellation of active work. An unstarted edited job can be explicitly
reapproved; executed jobs cannot be repurposed into another revision.

Quota belongs to the provider account pool. Fresh provider evidence and sufficient
local reservation capacity are both required for unattended admission. Unknown,
stale, malformed, or insufficient quota waits. The selected remote worker must
have a fresh authenticated heartbeat, no active ownership, and matching profile
and harness capabilities. Coding cannot fall back to a local worker.

## Operate the controller

The installed CLI accepts a trusted controller JSON file, using the profile,
worker, database, quota, and repository fields described below. File references
are resolved relative to that file, so restarting from another directory uses
the same durable state. Use one active controller for a database/account pool.

```bash
agentd github --config /path/to/controller.json approve 123 --actor operator
agentd github --config /path/to/controller.json serve
agentd github --config /path/to/controller.json status
```

`approve` fetches the exact current issue, verifies repository ID and eligibility,
and records revision-bound authority plus one queued job. It never launches a
worker. `serve` polls the allowlisted repository automatically and directly
refreshes known jobs even when they fall beyond discovery pages. A label alone
never authorizes coding. `status` reports durable job/run/publication outcomes
and draft URLs without contacting the worker or printing issue bodies.

The controller defaults to a 30-second tick (`poll_interval_seconds` overrides
it), emits changed status/publication reports, and uses authenticated heartbeats
and fresh account telemetry for admission. New remote-coding work forces a quota
refresh in the same tick it is discovered. Publication checks GitHub authority
again after validation and before external side effects. Read failures defer work.

Stop with SIGINT/SIGTERM and restart the same command/configuration to reconcile
existing runs and publication. Stopping the controller does not cancel or restart
remote coding. Worker runtime/token limits remain active. Registration preserves
remaining, reserved, and debt counters; restarting grants no extra quota.

Prepare `object_cache` as a trusted bare clone of the configured repository, and
keep it outside worker/model access. `unused_workspace_root` is required by the
existing scheduler interface; remote coding does not create local worktrees there.
Use a mode-0600 `worker.psk_file`, with TLS trust paths or an explicitly configured
loopback SSH tunnel. The controller uses the macOS sandbox on macOS and Bubblewrap
on Linux; unavailable containment fails publication closed. Run the documented
containment probe under the deployment identity before enabling a new layout.

The ordinary CLI needs no `issue_number`, build-evidence fields, or fault directory.
It never enables qualification fault injection. The quota command must return an
observation for `account_pool`; an observation for another pool is rejected.
Profile validation commands must be nonempty before any work is admitted.

For example, start from this configuration and replace the repository identity,
full base SHA, worker identity, and validation argv with deployment-specific values.
The quota command runs from the controller's working directory, so use an installed
executable or an absolute path. Its output must be a `ProviderQuotaSnapshot`, not
an estimated token balance. Example token limits are stopping thresholds, not
provider-enforced spending caps.

```json
{
  "profile": {
    "id": "repo",
    "version": "1",
    "repository": "owner/repo",
    "clone_url": "https://github.com/owner/repo.git",
    "validation_commands": [["git", "diff", "--check"]],
    "max_runtime_seconds": 180
  },
  "repository_id": 12345,
  "base_commit": "0000000000000000000000000000000000000000",
  "base_branch": "main",
  "account_pool": "codex",
  "expected_tokens": 50000,
  "maximum_tokens": 100000,
  "database": "controller.sqlite",
  "object_cache": "objects.git",
  "unused_workspace_root": "unused-workspaces",
  "quota_command": ["agentd", "codex-status", "--pool", "codex"],
  "worker": {
    "name": "coding-worker",
    "host": "127.0.0.1",
    "port": 38091,
    "node_id": "coding-worker",
    "session_epoch": "configured-worker-epoch",
    "psk_file": "worker.psk",
    "tls_ca": "worker-ca.crt",
    "server_hostname": "coding-worker"
  }
}
```

Add the repository's meaningful test/build commands to `validation_commands`;
`git diff --check` alone checks whitespace, not functional correctness. Install
their dependencies in the reviewed validation runtime before running the controller.

## Run the bounded qualification composition

`tools/qualify_issue_to_draft.py --config <protected-local-json> --approve-as
<operator-identity>` runs a bounded controller against an already deployed worker.
The JSON is trusted administrative policy and must not come from an issue. It
contains:

- `profile`: the complete serialized repository profile;
- `worker`: a `RemoteWorkerEndpoint` configuration, with protected PSK-file and
  TLS trust references rather than secret values;
- `repository_id`, `issue_number`, `base_commit`, and `base_branch`;
- `account_pool`, `expected_tokens`, and `maximum_tokens`;
- `database`, `object_cache` (a Git repository for verified bundle import), and
  `unused_workspace_root` (the coordinator interface's local path);
- `quota_command`: a bounded trusted command returning the existing serialized
  `ProviderQuotaSnapshot`, including its provider observation timestamp;
- `source_commit`, `worker_source_commit`, and `evidence_path` for exact build
  and run provenance; and optional `controller_timeout_seconds`.

The optional trusted `background_block_used_percent` setting selects the account
headroom policy for this controller (default 75; it must remain below the existing
90% urgent-only boundary). Only an operator can change this setting. It does not
waive fresh quota evidence, provider exhaustion checks, or finite job limits.
Qualification evidence records the configured threshold explicitly.

For controlled qualification only, `publication_fault_directory` enables one
lost-response injection after each successful branch push and draft creation.
The tool records which real side effect completed, then discards its response.
The normal publication reconciler must discover the existing branch/draft on
retry without repeating coding. Keep those markers with the qualification
evidence; omit this setting for ordinary operation.

The qualification composition currently uses the reviewed macOS validation
runner and the separately deployed Linux coding worker. It is an internal tool,
not a general deployment installer. The worker factory grants containment
capabilities only after the existing deployment sandbox probe succeeds. Other
worker layouts require an independently proven containment adapter; profile
portability does not establish host isolation.

Restart with the same JSON and database **without** `--approve-as` to reconcile
existing authority and ownership. Preserve worker state, workspace roots, node
identity and session epoch. A controller timeout does not authorize a new run.
Unknown execution ownership keeps worker capacity reserved. A proven persisted
terminal SDK result may be collected after worker restart without starting or
resuming the model; ambiguous active ownership remains unresolved.

Worker-local envelope preparation is atomic and reuses the shared worker node.
A preparation failure before the provider boundary is terminal with zero usage.
For legacy partial envelopes, an explicit trusted maintenance call to
`CodingHarnessDriver.reconcile_preparation_failure` can record failure only after
the worker is quiescent and the SDK proves both its run and session absent, with
matching retained job/workspace identities. It requires the deployed source
revision, retains audit evidence, and never starts a provider. This maintenance
operation is not exposed through worker STATUS or an issue-controlled API.

## Validate, publish, and retain evidence

A completed coding run enters `REVIEW`. The trusted publisher verifies source
approval, worker/run/profile identities, exact base and result commits, bundle
integrity, ancestry, and contained process validation. A model statement that
checks passed is not validation evidence. The coding harness receives no GitHub
publication credentials.

Publication has a separate durable ledger. Retry push or draft creation through
`CodingPublicationReconciler`; do not retry execution. A successful push with a
lost response reconciles the intended branch SHA. Draft discovery requires the
same repository, marker, base and head. An uncertain PR-creation response without
a matching visible draft stays pending instead of blindly posting again.

Keep controller and worker databases, claim/workspace/result manifests, exact
build identities, and validation evidence until review and retention policy permit
cleanup. `CodingHarnessDriver.release(run_id)` removes only resolved run working
files and bundle data, retaining claim/result evidence. It rejects unresolved
ownership. Do not erase journals to make a held worker appear idle.

The token maximum is an observed-usage stopping threshold, not a provider-enforced
spend cap: one provider batch can cross it before telemetry arrives. Record all
actual consumption and the overshoot flag. See [quotas](../20-using-agentd/quotas.md)
and the [remote coding decision](../70-decisions/0005-capability-limited-remote-coding.md).
