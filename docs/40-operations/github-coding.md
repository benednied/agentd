# Authorized GitHub coding

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
