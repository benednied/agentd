# Recovery

Managed Codex recovery preserves durable intent and thread state; it does not
resume an interrupted OS process, old stdio transport, or exact turn. Remote
artifact operations have a stricter boundary: the worker journal makes retries
idempotent where a response is durable, but an in-flight operation after worker
restart is not magically resumed.

At startup, and during every normal reconciliation tick, the daemon reconciles
durable `ADMITTED`/`STARTING`, `RUNNING`, `DRAINING`, and `CHECKPOINTED` runs. An
intent that never acquired a driver session is abandoned only when no remote
worker claim exists.
For managed local Codex runs, the SDK driver starts a new App Server process,
resumes the validated Codex thread in the existing worktree, and begins a recovery
turn. A missing remote backend is a hard failure; the coordinator never falls back
to a local harness or local workspace for a remote operation. A typed operation
left at the persisted `ADMITTED`/`STARTING` boundary is queried by its durable run
ID and is never started again. A durable worker claim in `claimed` or `started`
state is reported as `known=true` and nonterminal even after worker restart,
when the in-memory handle is unavailable; the same run ID and
reservation/allocation remain held for operator reconciliation. A known
terminal run can be finalized normally. An authenticated `known=false` status
is authoritative only when there is no durable claim, and then the coordinator
fails the attempt and releases its resources. Transport uncertainty, including
an uncertain START response or a START cancellation after the request may have
been sent, retains the intent and resources for a later status query; an
arbitrary driver START exception after claiming remains unresolved for the same
fail-closed reason.

Suspension follows this sequence:

```text
RUNNING -> DRAINING -> CHECKPOINTED -> SUSPENDED
                                      |
                                      +-> READY -> new run attempt
```

Turn-boundary adapters finish the steered turn before publishing a resume capsule;
native-pause adapters interrupt after the capsule is durable. The attempt is
collected, usage is recorded, capacity is released, and the Git lease remains.
Resume dispatch validates the lease and supplies the latest capsule in a new
contract.

There is no generic transactional outbox or recovery path for arbitrary process
adapters. SQLite cannot atomically commit a remote Docker/Compose/Git side effect
with the control-plane lifecycle record, so an orphan gap remains if the
controller crashes between them. A remote worker's pending journal row is treated
as an unknown side effect after worker restart and retries fail closed. The
durable run claim applies even when a retry changes its request ID, provided the
worker keeps the same configured durable session epoch. Deliberately rotating the
epoch creates a new idempotency namespace. The journal does not make that external
side effect SQLite-atomic. Cleanup after a terminal transaction is idempotent
where practical; cleanup errors do not roll back the accepted lifecycle result.
