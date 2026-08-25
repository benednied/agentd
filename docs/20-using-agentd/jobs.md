# Jobs and lifecycle

A job contains caller-supplied intent: project, repository, objective, acceptance
criteria, dependencies, effort estimate, QoS, resource requirements, capabilities,
and quota budgets. `agentd` does not infer this information from issues or plans.

## Submission

Submission persists a job in `BACKLOG` and normally moves it to `READY`. Jobs that
need reconnaissance enter `PLANNING` and receive a bounded reconnaissance child.
The job remains `READY` until dependencies, quota, compatible capacity, a matching
harness, and a local backend are available.

## Dispatch behavior

For an ordinary job, the coordinator:

1. checks dependency and gang readiness;
2. orders candidates deterministically;
3. reserves the expected quota path;
4. accounts resources against a compatible logical node;
5. validates or creates an exclusive Git worktree lease;
6. persists admission and a compact execution contract; and
7. starts the selected driver on the current host.

Failed starts compensate acquired resources only after the process is known to be
quiescent. The worker receives the execution contract, not quota balances, node
identity, scarcity, QoS rank, or scheduler reasoning.

## Managed completion

Managed Codex work streams observations and cumulative usage into durable state.
Valid terminal telemetry creates a trusted handoff commit and moves the job to
`REVIEW`. A caller or operator then accepts the result or requests a bounded repair
turn. `agentd` never merges the handoff.

A completed, quiescent suspended checkpoint can instead be promoted to `REVIEW`
after independent operator validation with the `review` operation.

Generic drivers may complete directly. If terminal usage is absent or inconsistent,
the job enters `METERING_PENDING`, capacity is released, and acceptance remains
blocked instead of guessing a charge.

See the [exact transition table](../80-reference/state-machine.md) and the
[review guide](review-and-repair.md).
