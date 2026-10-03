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

## GitHub coding attempt budgets

Unattended coding repair and checkpoint continuation share three bounded limits:

- `maximum_automatic_attempts` limits coding attempts, including the initial
  provider execution and later repair or continuation attempts. Its default is
  three.
- `maximum_preparation_attempts` limits retries of proven failures before provider
  start. Its default is three. Historical preparation failures do not consume the
  coding limit after a coding run has started successfully.
- `maximum_total_attempts` caps all retained attempts. By default it is the sum
  of the two limits; an operator may set a smaller overall cap.

A preparation failure is counted separately only when the authenticated stopped
run proves that preparation failed before provider start, telemetry is valid, and
both usage and charged quota are zero. Missing or contradictory evidence counts
as a coding attempt. Existing runs and their accounting are retained unchanged.

GitHub status reports show both counters and the overall cap. A failed validation
can queue a coding repair even when earlier preparation failures used the
preparation allowance. `/agentd retry` uses these same limits and cannot raise
them. Exhaustion is reported as a blocker rather than suggesting that another
retry command overrides policy. Source authorization, worker ownership, and
cumulative token limits still apply independently of the attempt counters.
