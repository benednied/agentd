# State machine reference

Every job transition requires a non-empty reason. SQLite rechecks the transition
against persisted state and records the snapshot plus append-only transition in one
transaction.

| From | Allowed destinations |
| --- | --- |
| `BACKLOG` | `PLANNING`, `READY`, `CANCELLED` |
| `PLANNING` | `READY`, `BACKLOG`, `FAILED`, `CANCELLED` |
| `READY` | `ADMITTED`, `BACKLOG`, `CANCELLED` |
| `ADMITTED` | `RUNNING`, `READY`, `FAILED`, `CANCELLED` |
| `RUNNING` | `DRAINING`, `CHECKPOINTED`, `METERING_PENDING`, `SUSPENDED`, `REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `DRAINING` | `RUNNING`, `CHECKPOINTED`, `METERING_PENDING`, `FAILED`, `CANCELLED` |
| `CHECKPOINTED` | `RUNNING`, `METERING_PENDING`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `METERING_PENDING` | `REVIEW`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `SUSPENDED` | `READY`, `REVIEW`, `FAILED`, `CANCELLED` |
| `REVIEW` | `RUNNING`, `METERING_PENDING`, `COMPLETED`, `FAILED`, `CANCELLED` |
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
