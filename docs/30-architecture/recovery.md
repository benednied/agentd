# Recovery

Managed recovery preserves durable intent and thread state; it does not resume an
interrupted OS process, old stdio transport, or exact turn.

At startup the daemon reconciles durable `STARTING`, `RUNNING`, `DRAINING`, and
`CHECKPOINTED` runs. An intent that never acquired a driver session is abandoned.
Otherwise the managed SDK driver starts a new App Server process, resumes the
validated Codex thread in the existing worktree, and begins a recovery turn.

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
adapters. Cleanup after a terminal transaction is idempotent where practical;
cleanup errors do not roll back the accepted lifecycle result.
