# Control plane

## Ownership boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| Pure scheduling modules | Readiness, deterministic ordering, placement, and policy decisions | SQLite, Git, subprocesses |
| `SchedulerCoordinator` | Admission effects, compensation, and lifecycle sequencing | Harness command syntax or project authoring |
| SQLite state store | Runtime snapshots, transitions, reservations, allocations, telemetry, and durable commands | Repository truth or live App Server transports |
| Git workspace manager | Exclusive branch/worktree leases and commit inspection | Review acceptance, merges, or integration policy |
| Worker backend | How and where a selected driver is physically started | Which job, node, harness, or model wins |
| Harness driver | Execution-contract translation and run control | Scheduler quota, QoS, scarcity, or preemption |
| Run supervisor | App Server transport, streamed observations, usage normalization, and command delivery | Admission, repair count, review acceptance, or provider policy |
| Account oracle | Provider windows, reset, and opaque-credit observations | Local token balances or admission decisions |
| `AgentDaemon` | Startup recovery, provider polling, managed reconciliation, and dispatch | Repository intent, review judgment, or integration |

`WorkerNode` and `WorkerBackend` are deliberately separate. Placement selects a
node for compatibility and accounting; a backend turns that assignment into
physical execution. The MVP has only `LocalWorkerBackend`.

## Admission effect ordering

For a `READY` job, `dispatch_next` orders candidates, requires dependency and gang
readiness, checks quota/node/harness/backend compatibility, reserves quota,
allocates node resources, validates or creates a Git lease, persists admission,
persists a starting run and contract, starts the driver, and finally records the
run as `RUNNING`.

Git and App Server effects cannot share a SQLite transaction. Intermediate state is
therefore durable and failures are compensated. If a started driver cannot be
shown to be quiescent, allocations and reservations remain held rather than
risking two live owners. Cleanup errors are attached to the original failure.

## APIs

`ControlPlane` is the transport-independent administrative/application facade. It
submits and inspects jobs, manages nodes and quota, and delegates lifecycle and
review commands to the coordinator. `AgentAPI` exposes run-scoped worker actions;
the run ID acts as an opaque bearer capability. See [Python API](../80-reference/python-api.md).
