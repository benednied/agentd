# State machine reference

Every job transition requires a non-empty reason. SQLite rechecks the transition
against persisted state and records the snapshot plus append-only transition in one
transaction.

| From | Allowed destinations |
| --- | --- |
| `BACKLOG` | `PLANNING`, `READY`, `CANCELLED` |
| `PLANNING` | `READY`, `BACKLOG`, `FAILED`, `CANCELLED` |
| `READY` | `ADMITTED`, `BACKLOG`, `CANCELLED` |
| `ADMITTED` | `RUNNING`, `READY`, `METERING_PENDING`, `FAILED`, `CANCELLED` |
| `RUNNING` | `DRAINING`, `CHECKPOINTED`, `METERING_PENDING`, `SUSPENDED`, `REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `DRAINING` | `RUNNING`, `CHECKPOINTED`, `METERING_PENDING`, `FAILED`, `CANCELLED` |
| `CHECKPOINTED` | `RUNNING`, `METERING_PENDING`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `METERING_PENDING` | `REVIEW`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `SUSPENDED` | `READY`, `REVIEW`, `FAILED`, `CANCELLED` |
| `REVIEW` | `READY`, `RUNNING`, `METERING_PENDING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `COMPLETED` | none |
| `FAILED` | `SUSPENDED` |
| `CANCELLED` | `SUSPENDED` |

Managed Codex normally uses `RUNNING -> REVIEW -> COMPLETED`. A repair uses
`REVIEW -> RUNNING -> REVIEW`; a suspension uses
`RUNNING -> DRAINING -> CHECKPOINTED -> SUSPENDED -> READY`.

Typed remote coding can go directly from `RUNNING` to `SUSPENDED` after the
worker proves terminal ownership and captures a trusted checkpoint. Explicit
administrative recovery can restore `FAILED`/`CANCELLED` coding jobs to
`SUSPENDED` with matching checkpoint evidence and reconciled accounting. The
original terminal run and result remain unchanged; resuming creates a new run.

A bounded remote coding repair uses `REVIEW -> READY -> ADMITTED -> RUNNING`
after terminal ownership, usage, and checkpoint provenance are proven. A trusted
GitHub retry can apply the same proof checks to stopped `FAILED` or `CANCELLED`
jobs, restoring them through `SUSPENDED` without changing the original run.

After an authenticated GitHub abandonment, a host-fenced unknown attempt uses
job state `METERING_PENDING` and run state `QUARANTINED`. This run state records
physical retirement only: its result and final usage remain unresolved. The
allocation is released atomically with an immutable retirement audit, while
the outstanding quota reservation, usage samples, pool balance, and retained
workspace remain. The same attempt cannot restart, retry, publish, or obtain a
refund through issue closure; fresh independent jobs may use physical capacity
and the remaining unreserved account allowance.
