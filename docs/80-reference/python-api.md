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

For exact models and protocol fields, inspect the typed definitions in
`src/agentd/domain/models.py` and `src/agentd/workers/protocol.py`.
