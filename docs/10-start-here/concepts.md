# Concepts

`agentd` is a local meta-harness. It owns scheduling, resources, workspace
isolation, lifecycle, and review handoff. A harness owns execution of the
assignment but does not choose priority, quota, workspace, or lifecycle.

## Main path

```text
Caller -> ControlPlane / AgentDaemon
       -> readiness -> quota -> logical placement
       -> LocalWorkerBackend -> HarnessDriver (local coding run)
       -> RemoteWorkerBackend -> typed Build/Deploy operation (remote artifact run)
       -> checkpoint / metering / review / repair / acceptance
```

Placement selects a `WorkerNode` for compatibility and accounting. A
`LocalWorkerBackend` starts ordinary harnesses on the controller host. A
`RemoteWorkerBackend` addresses a node-bound authenticated worker, but the
coordinator admits remote execution only for typed Build/Deploy operations. A
remote operation receives immutable artifact inputs and a logical `worker://`
workspace; it does not receive a controller-local Git worktree.

## Responsibilities

| Component | Owns | Does not own |
| --- | --- | --- |
| Repository | Source, plans, issues, acceptance, and accepted decisions | Live runs, quota, allocations |
| Scheduling policies | Readiness, ordering, placement, and policy decisions | SQLite, Git, subprocesses |
| Scheduler coordinator | Admission effects, compensation, and lifecycle sequencing | Harness command syntax or project authoring |
| SQLite | Runtime snapshots, audit, reservations, allocations, telemetry, and commands | Repository truth or live transports |
| Git workspace manager | Exclusive branch/worktree leases and commit inspection | Review acceptance or merges |
| Worker backend | How and where a selected driver starts | Which job or node wins |
| Harness driver | Execution-contract translation and run control | Quota, QoS, scarcity, or preemption |

## Job lifecycle

The common managed path is:

```text
BACKLOG -> READY -> ADMITTED -> RUNNING -> REVIEW -> COMPLETED
                                  |          |
                                  |          +-> RUNNING (repair) -> REVIEW
                                  +-> CHECKPOINTED -> SUSPENDED -> READY (resume)
                                                        |
                                                        +-> REVIEW (validated checkpoint)
RUNNING -> METERING_PENDING when terminal usage cannot be trusted
```

The [using guide](../20-using-agentd/jobs.md) describes observable behavior. The
[state-machine reference](../80-reference/state-machine.md) is the exact list of
allowed transitions.
