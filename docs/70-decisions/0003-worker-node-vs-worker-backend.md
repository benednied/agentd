# ADR 0003: Separate worker nodes from worker backends

- Status: Accepted
- Date: 2026-08-25

## Context

Scheduling needs to reason about operating systems, architectures, labels,
capabilities, resources, harnesses, and model classes. Physical execution may later
use a different transport, but the local MVP must not imply that a logical node is
already a remote worker.

## Decision

Keep `WorkerNode` as logical compatibility and accounting metadata. Keep
`WorkerBackend` as the mechanism that starts the selected harness. Placement selects
the former; the latter performs execution. The MVP implements only
`LocalWorkerBackend`.

## Alternatives

- Combine node and backend: rejected because scheduling policy would be coupled to
  transport and could incorrectly promise remote execution.
- Make every registered node a process worker: rejected because registration is
  durable metadata, not worker provisioning.
- Add a remote protocol now: deferred until the local lifecycle and security
  invariants have a supported transport requirement.

## Consequences

The architecture can add a remote backend without changing placement policy. In
the current MVP, selected nodes account capacity but all harnesses run on the
controller host.
