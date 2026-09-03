# ADR 0003: Separate worker nodes from worker backends

- Status: Accepted
- Decision date: Predates ADR repository
- Recorded: 2026-08-25
- Supersedes: None
- Superseded by: None

## Context

Scheduling needs to reason about operating systems, architectures, labels,
capabilities, resources, harnesses, and model classes. A logical node must not be
presented as a remote worker merely because it is registered; remote execution
requires a compatible, authenticated backend.

## Decision

Keep `WorkerNode` as logical compatibility and accounting metadata. Keep
`WorkerBackend` as the mechanism that starts the selected harness or typed worker
operation. Placement selects the former; the latter performs execution. The MVP
implements `LocalWorkerBackend` plus a node-bound authenticated remote backend for
typed artifact Build/Deploy operations.

## Alternatives

- Combine node and backend: rejected because scheduling policy would be coupled to
  transport and could incorrectly promise remote execution.
- Make every registered node a process worker: rejected because registration is
  durable metadata, not worker provisioning.
- General remote coding-agent execution: rejected for this MVP. The remote
  protocol is intentionally restricted to typed artifact operations.

## Consequences

Placement remains independent of transport. Local harnesses run on the controller
host; allowlisted typed artifact operations may run on a node-bound remote worker.
There is no HA scheduler, Kubernetes fleet manager, or automatic continuation of a
worker process after worker restart.
