# Glossary

**Agent API** — the run-scoped worker interface that exposes assignment,
checkpoint, refinement, blocker, review, and completion operations.

**Agent daemon** — the long-running service that recovers managed runs, polls
provider telemetry, reconciles commands, and dispatches ready jobs.

**Execution contract** — the bounded data sent to a harness for one run.

**Harness** — an adapter that executes an assignment, such as the fake driver,
managed Codex SDK driver, or legacy CLI driver.

**Job** — caller-supplied intent plus dependencies, acceptance, effort, resource,
QoS, and quota requirements.

**Logical node** — a `WorkerNode` record used for compatibility and accounting. It
does not imply a remote machine.

**Lease** — an exclusive Git branch and linked worktree owned by a job.

**Managed run** — a run with durable driver session, observations, usage, and
recoverable commands, currently provided by the Codex SDK path.

**Review handoff** — a trusted commit and persisted result awaiting operator
acceptance; it is not a merge.

**Worker backend** — the mechanism that starts a selected harness. The MVP backend
is local-process only.
