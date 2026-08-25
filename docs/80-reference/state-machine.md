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
| `RUNNING` | `DRAINING`, `CHECKPOINTED`, `METERING_PENDING`, `REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `DRAINING` | `RUNNING`, `CHECKPOINTED`, `METERING_PENDING`, `FAILED`, `CANCELLED` |
| `CHECKPOINTED` | `RUNNING`, `METERING_PENDING`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `METERING_PENDING` | `REVIEW`, `FAILED`, `CANCELLED` |
| `SUSPENDED` | `READY`, `FAILED`, `CANCELLED` |
| `REVIEW` | `RUNNING`, `METERING_PENDING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `COMPLETED` | none |
| `FAILED` | none |
| `CANCELLED` | none |

Managed Codex normally uses `RUNNING -> REVIEW -> COMPLETED`. A repair uses
`REVIEW -> RUNNING -> REVIEW`; a suspension uses
`RUNNING -> DRAINING -> CHECKPOINTED -> SUSPENDED -> READY`.
