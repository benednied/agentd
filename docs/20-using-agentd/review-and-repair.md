# Review and repair

Managed Codex completion is a review handoff, not automatic integration.

After a valid terminal turn, the coordinator records the trusted worktree `HEAD`,
releases node capacity, settles usage, and moves the job to `REVIEW`. An operator
can accept the result or request a repair instruction.

Repair is a durable command against the reviewed run. The daemon revalidates the
retained worktree, account policy, token maximum, and local capacity, then starts a
new turn on the same Codex thread. At most two repair turns are allowed. A
successful repair returns to `REVIEW`; acceptance moves the job to `COMPLETED`.

Suspension is a durable handoff:

```text
RUNNING -> DRAINING -> CHECKPOINTED -> SUSPENDED -> READY -> new run
                                      |
                                      +-> REVIEW (after independent validation)
```

The driver stops at a safe boundary, publishes a resume capsule, records usage,
releases capacity, and retains the Git lease. Resuming creates a new run attempt
with the latest capsule. The `review` operation promotes only a completed,
quiescent checkpoint after independent validation. Recovery resumes intent, not the
interrupted process.
