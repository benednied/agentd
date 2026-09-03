# Python API reference

`ControlPlane` is the transport-independent application facade. It submits and
inspects jobs, manages nodes and quota snapshots, and exposes lifecycle, review,
repair, and administrative operations.

`AgentAPI` is run-scoped. The run ID is an opaque bearer capability, and stale
attempts are rejected. Worker operations are:

- `get_assignment`
- `request_refinement`
- `report_blocker`
- `checkpoint`
- `request_review`
- `complete`
- `request_history` for retained requests

The returned `ExecutionContract` contains the assignment, acceptance criteria,
dependency handoffs, allowed filesystem scope, model class, and completion or
checkpoint protocol. It excludes node identity, quota balance, QoS rank, scarcity,
and scheduler rationale.

The `AgentDaemon` performs managed-driver recovery, provider polling, live usage
reconciliation, pending command delivery, repair startup, and repeated dispatch.
`create_local_runtime` composes the portable fake path used by examples and tests.

## `ControlPlane`

The facade is constructed with a `StateStore` and, for lifecycle operations, an
injected `LifecycleCoordinator`. Query and registration methods are synchronous;
driver and lifecycle operations are asynchronous where shown.

| Method | Purpose |
| --- | --- |
| `submit(job)` | Persist a `BACKLOG` job and queue it for normal or reconnaissance planning. |
| `inspect_job(job_id)` / `list_jobs(states=None)` | Read one or many job snapshots. |
| `history(job_id)` | Read append-only state transitions. |
| `register_node(node)` / `list_nodes()` | Register and inspect logical worker nodes, declared capacity, and persisted heartbeat. |
| `register_quota_pool(pool)` / `inspect_quota(pool_id)` | Register and inspect quota pools. |
| `register_quota_event(event)` | Apply a quota reset event. |
| `inspect_workspace(job_id)` | Read the job's workspace lease, if any. |
| `runs(job_id)` / `checkpoints(job_id)` | Read durable run and checkpoint records. |
| `request_repair(job_id, instruction)` | Enqueue one bounded same-thread repair command. |
| `return_to_backlog(job_id)` / `requeue(job_id)` | Move eligible work through backlog states. |
| `degrade(job_id, harness, model_class)` | Change the selected fallback preferences. |
| `evaluate_tail(job_id, consumed)` | Apply the deterministic tail-governor policy. |
| `dispatch_next()` | Admit and start the next ready job. |
| `checkpoint(job_id, capsule)` / `pause(job_id, capsule)` | Persist a checkpoint or suspend work. |
| `resume(job_id)` / `cancel(job_id)` | Resume or cancel lifecycle work. |
| `request_review(job_id)` / `complete(job_id)` / `accept(job_id)` | Enter review, finalize, or accept a result. |
| `recover_managed_runs()` / `reconcile_managed_runs(...)` | Recover and reconcile durable managed-driver runs. |
| `apply_provider_snapshot(snapshot)` | Apply an externally observed provider quota snapshot. |
| `refresh_worker_heartbeats()` | Authenticate configured remote workers, gate backend health, and persist the latest status on each bound node. |
| `assignment(run_id)` | Return the worker-visible `ExecutionContract`. |

`accept(job_id)` is the explicit human-review operation. It requires `REVIEW`;
completion is not an implicit merge or deployment.

## `AgentAPI`

`AgentAPI` is the run-scoped worker protocol. The `run_id` is an opaque bearer
capability and mutating calls must name the current run for the job.

| Method | Result / contract |
| --- | --- |
| `get_assignment(run_id)` | Defensive copy of the `ExecutionContract`. |
| `request_refinement(run_id, question)` | Durable bounded refinement Agent Request. |
| `report_blocker(run_id, blocker, retryable=True)` | Durable bounded blocker Agent Request. |
| `checkpoint(run_id, capsule)` | Durable `CheckpointResult`; asynchronous. |
| `request_review(run_id)` | `AgentActionResult` with `review-requested`; asynchronous for unmanaged runs. |
| `complete(run_id)` | `AgentActionResult` with `complete`; asynchronous for unmanaged runs. |
| `request_history(run_id)` | Retained request records for this run only. |

Managed runs finalize through observation reconciliation, so their worker-facing
`request_review` and `complete` lifecycle calls are rejected rather than
short-circuiting durable telemetry.

## Typed records

The [domain model reference](domain-models.md) is the normative field reference
for `Job`, `ArtifactRef`, `ArtifactSelector`, `ArtifactSpec`,
`BuildImageOperation`, `DeployImageOperation`, `ExecutionContract`, `WorkerNode`,
`WorkerHeartbeat`, `ResourceVector`, `QuotaPool`, `QuotaBudget`,
`QuotaReservation`, `WorkspaceLease`, `Checkpoint`, `ResumeCapsule`, `RunRecord`,
`RunObservation`, `ProducedArtifact`, and `RunResult`. It also lists
the persisted values for all important enums. The [worker backend protocol](../../src/agentd/workers/protocol.py)
defines `WorkerBackendCapabilities` and the `WorkerBackend` protocol.
