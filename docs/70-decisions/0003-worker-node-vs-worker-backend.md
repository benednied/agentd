# ADR 0003: Separate worker nodes from worker backends

- Status: Accepted
- Decision date: Predates ADR repository
- Recorded: 2026-08-25
- Supersedes: None
- Superseded by: None

## Context

Scheduling needs to reason about operating systems, architectures, labels,
capabilities, resources, harnesses, and model classes. The current implementation
executes locally, so a logical node must not be presented as a remote worker.

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
- Add a remote execution protocol: out of scope for the current product boundary;
  this repository contains no roadmap item for it.

## Consequences

Placement remains independent of the current local transport. Selected nodes
account capacity, but all harnesses run on the controller host; this ADR does not
promise or plan remote execution.
