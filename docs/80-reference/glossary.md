# Glossary

**Agent API** — the run-scoped worker interface that exposes assignment,
checkpoint, refinement, blocker, review, and completion operations.

**Agent daemon** — the long-running service that recovers managed runs, polls
provider telemetry, reconciles commands, and dispatches ready jobs.

**Execution contract** — the bounded data sent to a harness for one run.

**Gang readiness** — the MVP barrier rule that holds every job in a gang until
each known member is dependency-ready. It does not provide an atomic or
distributed launch.

**Harness** — an adapter that executes an assignment, such as the fake driver,
managed Codex SDK driver, or legacy CLI driver.

**Job** — caller-supplied intent plus dependencies, acceptance, effort, resource,
QoS, and quota requirements.

**Logical node** — a `WorkerNode` record used for compatibility and accounting. It
does not imply a remote machine.

**Lease** — an exclusive Git branch and linked worktree owned by a job.

**Hors-categorie** — the unbounded QoS class. It cannot dispatch directly; the
control plane creates bounded reconnaissance work before promotion to a bounded
class.

**Interactive reserve** — quota capacity protected for interactive and blocker
work. It is represented by `QuotaPool.minimum_interactive_reserve`.

**Managed run** — a run with durable driver session, observations, usage, and
recoverable commands, currently provided by the Codex SDK path.

**METERING_PENDING** — a job or reservation state used when terminal usage is
missing, invalid, inconsistent, or not yet settled. The system does not guess a
provider charge while it is pending.

**Provider snapshot** — an append-only observation of provider rate-limit windows,
reset times, reached flags, and opaque credit payloads. It is not a fabricated
absolute token balance.

**Reconnaissance** — a bounded planning slice for a `hors-categorie` job. It
identifies unknowns, dependencies, estimates, and safe checkpoint boundaries
before normal execution is admitted.

**Review handoff** — a trusted commit and persisted result awaiting operator
acceptance; it is not a merge.

**Repair turn** — one bounded same-thread continuation requested while a job is in
`REVIEW`. The current implementation permits at most two repair turns.

**Resume capsule** — a compact checkpoint containing completed work, current work,
next steps, known failures, decisions, and optionally a commit. It is the durable
handoff used to resume without replaying the full conversation.

**Tail governor** — the deterministic effort-overrun policy that continues through
`p90`, requests re-estimation, checkpoints and replans significant overruns, and
converts runaway work to `hors-categorie`.

**Worker backend** — the mechanism that starts a selected harness. The MVP backend
is local-process only.

**Burn** — bounded, checkpointable work promoted during `PRE_RESET_BURN` quota
 mode while preserving urgent interactive capacity.
