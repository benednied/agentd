# ADR 0005: Capability-limited remote coding (issues #50, #60, #64)

- Decision date: 2026-09-18
- Recorded: 2026-09-18

## Decision

Support one trusted controller's authorized coding work through the existing
worker START/OBSERVE/STATUS/CANCEL/COLLECT protocol. A `CodingOperation` contains
only a portable `CodingWorkOrder`; it has no command, local path, credential or
publication field. `CodingHarnessDriver` (`remote-coding`) is an opt-in adapter
around a worker-configured managed harness. No new listener or shell endpoint
is introduced.

The worker independently checks the profile digest/version, repository, harness,
account binding, capabilities and runtime limits. It materializes a detached Git
worktree at the exact base SHA in a lease unique to the durable run ID. The
existing GitWorkspaceManager owns the editing branch and trusted commit capture. It
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

The existing reviewed deployment provides this boundary using its image-pinned
custom permission profile and audited bubblewrap shim. The trusted bootstrap
`create_verified_coding_sdk` executes the existing
`deploy/security/runtime_sandbox_probe.py` before granting credential isolation
and network-disabled features to that SDK instance. Failure aborts startup. The
probe checks actual credential/state read denial, sibling workspace isolation,
outside-write denial and network denial using the pinned SDK's generated command
path. The factory restricts runtime environment and state to the reviewed
container layout. SDK startup uses an explicit `env -i` launch because the
pinned SDK otherwise merges host environment into its supplied environment.
A generic SDK elsewhere still has no containment features.
Publication credentials belong exclusively to the trusted controller finalizer.
Real provider-backed end-to-end qualification remains tracked in #66.

## Ownership and reconciliation

Existing worker journal claims precede the adapter start. A lost acknowledgement
replays the request or the same in-memory run, never starts a second provider
turn. Claims survive worker process restart. A completed adapter result is
fsynced before it becomes visible, then imported into the worker journal on
STATUS/COLLECT. A terminal result can be collected with a fresh request after
controller or worker restart.

A completed SDK turn whose adapter result was not yet persisted can be
reconciled from its durable terminal observation and original claim/workspace
manifest. This trusted path only collects/commits existing files; it never starts
or resumes a provider turn. A terminal token sample can exceed the ceiling by
one provider step; actual usage and the overshoot flag are retained, and an
already-completed provider is never cancelled. A failed stop acknowledgement
without terminal proof retains unresolved ownership.

An active run whose process ownership cannot be proven after worker restart
remains known/nonterminal. It cannot be restarted or cancelled speculatively.
An operator must resolve the worker process before redispatch. This favors
safety over availability; active-turn reattachment is not implemented here.
Worker session epochs must remain stable for reconnect/restart recovery; epoch
rotation is an administrative ownership change, not a way to retry claims.

Runtime and cumulative token thresholds request cancellation of active work.
They are not a strict cap on provider spend: batched observations and in-flight
work can exceed the requested maximum before a stop takes effect. A proven
terminal success is preserved rather than cancelled retroactively; its full
usage is retained and collected evidence marks `quota_ceiling_exceeded` when
appropriate. Observation and usage are forwarded to the existing controller
governor. Cancellation does
not delete the lease. A lease is retained after success, failure, cancellation
or ambiguous setup. Explicit administrative `release(run_id)` is permitted only
for terminal leases and removes workspace/mirror/bundle while retaining claim
and result evidence. There is no remote arbitrary-delete operation.

## Trusted result boundary

Model summaries and claimed commit IDs are not validation evidence. On completed
coding the adapter requires valid SDK usage evidence, uses the existing trusted
GitWorkspaceManager to capture edits (the model does not need Git write
authority), reads actual Git HEAD, verifies base ancestry and a clean workspace,
and builds an incremental bundle. Before trusted collection, worker-owned mirror configuration is replaced with
minimal known policy, so model-modified includes or filter commands are never
interpreted, including during recovery of older leases. Git hooks, fsmonitor,
grafts and replacement objects are disabled; trusted Git subprocesses receive
an explicit environment. The result binds run/job/source
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
active ownership reattachment and deployment
resource/platform inventory remain necessary before claiming #64/#66 complete.

The verified SDK composition mirrors each controller-admitted work order into the
existing SQLite run/session/reservation/usage ledger before provider start. Its
local pool is explicitly a per-run execution envelope, never a provider quota
snapshot or a second admission policy. The controller remains the sole account
admission and final accounting authority. Terminal cancellation/failure retains
observed usage; missing trustworthy usage is marked telemetry-invalid so the
controller can keep metering unresolved rather than treating it as zero.
