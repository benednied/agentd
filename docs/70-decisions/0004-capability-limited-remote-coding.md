# Capability-limited remote coding (issues #50, #60, #64)

## Decision

Support one trusted controller's authorized coding work through the existing
worker START/OBSERVE/STATUS/CANCEL/COLLECT protocol. A `CodingOperation` contains
only a portable `CodingWorkOrder`; it has no command, local path, credential or
publication field. `CodingHarnessDriver` (`remote-coding`) is an opt-in adapter
around a worker-configured managed harness. No new listener or shell endpoint
is introduced.

The worker independently checks the profile digest/version, repository, harness,
account binding, capabilities and runtime limits. It materializes a detached Git
worktree at the exact base SHA in a lease unique to the durable run ID. It
rebuilds the local contract and drops the controller environment, filesystem
scope and publication completion instructions. Repository instructions stay in
the checked-out tree.

## Containment and deployment boundary

The adapter is **not** an operating-system sandbox. A supplied managed harness
must implement and advertise `credential-isolated`, `restricted-workspace-write`
and the profile's `network-disabled` or `network-provider-only` policy. These
features are an operator contract, not security enforced by Python feature
names. The stock Codex SDK driver intentionally does not advertise credential
isolation; it therefore cannot enable this operation out of the box.

Production enablement requires a demonstrated OS/container boundary plus a
provider credential broker (or equivalent) such that model tools cannot read
provider credentials, controller PSKs, SSH identities or publication credentials.
A sandbox allowing arbitrary host reads or a container with a readable provider
authentication file is insufficient. Publication credentials belong exclusively
to the trusted controller finalizer. Infrastructure-specific containment and
real provider qualification remain open under #64/#66; this increment must not
be represented as a production-qualified deployment.

## Ownership and reconciliation

Existing worker journal claims precede the adapter start. A lost acknowledgement
replays the request or the same in-memory run, never starts a second provider
turn. Claims survive worker process restart. A completed adapter result is
fsynced before it becomes visible, then imported into the worker journal on
STATUS/COLLECT. A terminal result can be collected with a fresh request after
controller or worker restart.

An active run whose process ownership cannot be proven after worker restart
remains known/nonterminal. It cannot be restarted or cancelled speculatively.
An operator must resolve the worker process before redispatch. This favors
safety over availability; active-turn reattachment is not implemented here.
Worker session epochs must remain stable for reconnect/restart recovery; epoch
rotation is an administrative ownership change, not a way to retry claims.

Runtime and cumulative token ceilings cancel the managed harness. Observation
and usage are forwarded to the existing controller governor. Cancellation does
not delete the lease. A lease is retained after success, failure, cancellation
or ambiguous setup. Explicit administrative `release(run_id)` is permitted only
for terminal leases and removes workspace/mirror/bundle while retaining claim
and result evidence. There is no remote arbitrary-delete operation.

## Trusted result boundary

Model summaries and claimed commit IDs are not validation evidence. On completed
coding the adapter reads actual Git HEAD, verifies base ancestry and a clean
workspace, and builds an incremental bundle. Git hooks, fsmonitor and replacement
objects are disabled during trusted collection. The result binds run/job/source
revision/repository/profile/base/result SHA and bundle SHA-256. Bundles up to
256 KiB cross the existing authenticated result channel as bounded base64 chunks.
Larger or invalid results fail collection and retain the lease for inspection.
No branch is pushed and no PR is created by this adapter. Trusted controller
import, independent validation and idempotent draft publication belong to #65.

## Evidence and remaining work

`tests/workers/test_coding_worker.py` uses actual Git objects and a fake managed
provider to cover pinned materialization, hostile outer contract fields, profile
and capability denial, exact result/bundle collection, cancellation, runtime
limits, lost start acknowledgement, reconnect, terminal worker restart, unresolved
ownership, and retention. Existing authenticated worker transport tests cover
socket reconnect and request replay. Real provider-backed remote qualification,
provider credential containment, active ownership reattachment and deployment
resource/platform inventory remain necessary before claiming #64/#66 complete.
