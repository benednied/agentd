# Architecture

Agentd is a local meta-harness: it decides what may run and where, then gives a
bounded execution contract to a harness. Harnesses execute work; they do not own
quota, priority, node selection, workspace isolation, preemption, or job lifecycle.

```text
Repository intent (caller-supplied in the current MVP)
                         |
                         v
             ControlPlane / AgentDaemon
                         |
             pure scheduling policies
          readiness | QoS | quota | placement
             tail | burn | reconnaissance
                         |
                         v
             SchedulerCoordinator
       SQLite | GitWorkspaceManager | WorkerBackend
              CodexAccountOracle
                         |
                         v
                  HarnessDriver
          Fake | Codex SDK/App Server | codex-cli
```

## Ownership boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| Source repository | Source, plans/issues, dependencies, acceptance and accepted decisions | Live runs, quota, node allocations |
| Pure scheduling modules | Readiness, deterministic ordering, placement and policy decisions | SQLite, Git, subprocesses |
| `SchedulerCoordinator` | Admission effects, compensation and lifecycle sequencing | Harness command syntax or project authoring |
| SQLite state store | Runtime snapshots, job transition audit, reservations, allocations, managed-driver sessions, telemetry and durable commands | Repository truth or live App Server transports |
| Git workspace manager | Exclusive branch/worktree leases and commit inspection | Review acceptance, merges or integration policy |
| Worker backend | Where a selected driver starts | Which job, harness or model wins |
| Harness driver | Translation of `ExecutionContract` and run control | Scheduler quota, QoS, scarcity or preemption policy |
| Run supervisor | App Server thread/turn transport, streamed observations, usage normalization and command delivery | Admission, repair count, review acceptance or provider-account policy |
| Account oracle | Read-only provider window, reset and opaque-credit observations | Local token balances or admission decisions |
| `AgentDaemon` | Startup recovery, provider polling, managed-run reconciliation and repeated dispatch | Repository intent, review judgment or integration |
| `AgentAPI` | The six run-scoped worker operations | Administrative state and scheduler rationale |

The architectural source of truth for project intent is the repository. The MVP
does not yet include a Beads/issue/plan adapter, so callers currently construct
`Job` records and structured reconnaissance outcomes themselves. SQLite stores a
runtime snapshot of that intent; updating a job snapshot is not a substitute for
updating repository truth.

## Lifecycle and audit

The complete job transition table is explicit in `domain/transitions.py`:

| From | Allowed destinations |
| --- | --- |
| `BACKLOG` | `PLANNING`, `READY`, `CANCELLED` |
| `PLANNING` | `READY`, `BACKLOG`, `FAILED`, `CANCELLED` |
| `READY` | `ADMITTED`, `BACKLOG`, `CANCELLED` |
| `ADMITTED` | `RUNNING`, `READY`, `FAILED`, `CANCELLED` |
| `RUNNING` | `DRAINING`, `CHECKPOINTED`, `METERING_PENDING`, `REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `DRAINING` | `RUNNING`, `CHECKPOINTED`, `METERING_PENDING`, `FAILED`, `CANCELLED` |
| `CHECKPOINTED` | `RUNNING`, `METERING_PENDING`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `METERING_PENDING` | `REVIEW`, `FAILED`, `CANCELLED` |
| `SUSPENDED` | `READY`, `FAILED`, `CANCELLED` |
| `REVIEW` | `RUNNING`, `METERING_PENDING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `COMPLETED`, `FAILED`, `CANCELLED` | none |

Each job state change requires a non-empty reason. SQLite rechecks the transition
against the persisted state and commits the new job snapshot and append-only
`job_transitions` row in one transaction. Run, workspace, reservation and
allocation records are durable snapshots with stable IDs, but they are not a
generic append-only event log.

Generic drivers may still complete directly. A successful managed Codex turn uses
an explicit review gate:

```text
RUNNING -> REVIEW -> COMPLETED
              |
              +-> RUNNING (bounded repair turn) -> REVIEW

RUNNING -> METERING_PENDING  (terminal telemetry is incomplete or inconsistent)
```

The SDK supervisor persists a terminal observation containing the result only
after the turn stream ends. The coordinator requires a matching terminal token
sample, records the trusted worktree `HEAD`, releases node and quota capacity, and
stores the job in `REVIEW` with an ended `SUSPENDED` run record. A later explicit
`accept`/`complete` call uses that persisted result; it does not restart or collect
the completed turn.

A repair request is a durable command against the reviewed run. The daemon
revalidates the retained worktree, account policy, local token maximum and node
capacity, then transitions `REVIEW -> RUNNING` and starts another turn on the same
Codex thread. The run ID and cumulative usage history are retained. At most two
repair turns are allowed, and every successful repair returns to `REVIEW`.

If terminal usage is absent, internally inconsistent, or missing from the durable
ledger, the node allocation is released but the quota reservation enters
`METERING_PENDING`. Acceptance remains blocked instead of guessing a charge. There
is no automated provider-side settlement for this exceptional state.

Suspension is a durable handoff, not merely a paused process:

```text
RUNNING -> DRAINING -> CHECKPOINTED -> SUSPENDED
                                             |
                                             +-> READY -> new run attempt
```

The coordinator asks the driver to stop at a safe boundary. Turn-boundary adapters
finish the steered turn before the `ResumeCapsule` is published; native-pause
adapters are interrupted after the capsule is durable. In both cases the attempt
is collected, usage is recorded, and quota/node capacity is released before
publishing `SUSPENDED`. The Git workspace remains leased. `resume` moves the job to
`READY`; dispatch creates a new run whose contract contains the latest capsule and
reuses the validated lease.

## Admission and effect ordering

For jobs in `READY`, `dispatch_next` applies this sequence:

1. Order candidates deterministically and require dependency/gang readiness.
2. Require the named quota pool and a compatible node, harness, model and local
   backend.
3. Reserve the expected accepted-artifact quota path.
4. Allocate node resources.
5. Validate and reuse the job's lease, or create a Git branch/worktree.
6. Persist `READY -> ADMITTED`.
7. Persist a `STARTING` run with a pending handle and compact contract.
8. Start the selected driver through the worker backend. Managed drivers receive
   the already-persisted run ID and persist their external session before returning
   a real handle.
9. Atomically persist the run as `RUNNING` with `ADMITTED -> RUNNING`.

Quota and node allocation each use optimistic snapshot checks and an atomic SQLite
transaction. Partial unique indexes enforce at most one active quota reservation,
resource allocation and run, and one leased workspace, per job.

Git and App Server operations cannot share a SQLite transaction. Dispatch therefore
persists recoverable intermediate state and compensates failures. If a started
driver can be quiesced, the coordinator returns an admitted job to `READY` and
releases newly acquired resources. If quiescing fails, capacity is deliberately
retained rather than risking two live owners.

The daemon now reconciles managed Codex runs at startup. It enumerates durable
`STARTING`, `RUNNING`, `DRAINING`, and `CHECKPOINTED` runs, abandons an intent that
never acquired a driver session, and otherwise resumes the durable Codex thread in
the existing worktree through a new App Server process and recovery turn. It never
signals a stale PID or claims to reattach the old stdio transport. This recovery is
specific to the managed SDK driver; there is still no generic transactional outbox
or recovery path for arbitrary process adapters.

Terminal cleanup is deliberately after the terminal job/run transaction. Cleanup
errors do not roll back the accepted lifecycle result and are reported as a
`LifecycleError`. Release operations are idempotent where practical.

## Scheduling, quota and tails

The scheduler policy modules are pure and deterministic:

- Dependencies block until every referenced job is `COMPLETED`. Gang members are
  held until every known member is dependency-ready; multi-node gang launch is not
  atomic and there is no persisted barrier aggregate.
- QoS, explicit priority, age and stable job ID determine ordering. Urgent work
  remains ahead of pre-reset burn work.
- Placement filters node state, OS/architecture/labels, browser/desktop and generic
  capabilities, available CPU/RAM/GPU/VRAM, allowed harnesses, and exact advertised
  model-class strings. Harness preference wins first; normalized resource waste
  and stable IDs provide deterministic best fit.
- Hors-categorie submission creates one bounded reconnaissance child and leaves
  the parent in `PLANNING`. Explicit promotion requires completed reconnaissance
  plus a structured finite plan, checkpoint boundaries, effort tail and budget
  cap.
- The tail governor maps measured effort to continue, re-estimate,
  checkpoint/replan or convert-to-hors-categorie decisions. It is callable through
  `ControlPlane.evaluate_tail`; no usage loop applies decisions automatically.

A quota reservation covers:

```text
implementation + review + likely repair + validation
```

It intentionally does not reserve the whole theoretical p99 tail. For ordinary
work, dispatchable quota is `remaining - active reservations - interactive
reserve`; interactive and blocker work may consume the reserve.

Quota has two deliberately separate evidence planes:

- Local `QuotaPool` and `QuotaReservation` records use either abstract units or
  tokens. A Codex job must use tokens and declare a cumulative maximum.
- `ProviderQuotaSnapshot` records retain App Server's primary/secondary used
  percentages, window durations and reset times, reached state, plan type and
  opaque credits. These signals are never converted into an absolute token
  balance.

The SDK supervisor derives each turn's usage from the App Server cumulative thread
total minus durable prior-turn baselines. Applying a sample atomically stores the
deduplicated `(run, thread, turn, sequence)` observation and charges only its
positive delta. A final marker may repeat the last cumulative reading at zero
additional charge. The result's legacy abstract `consumed_quota` stays zero;
typed token usage and the ledger are authoritative.

On every daemon tick, live policy may top up an active reservation at 80%, request
a durable checkpoint at 90% of the job maximum, and request an interrupt after the
hard cap's configured grace period. Provider policy blocks speculative/scavenger
work at 75% used, permits only interactive/blocker admission at 90%, and
checkpoints all active work when a limit or credits are exhausted. Stale provider
telemetry permits only urgent admission. A fresh reset within 12 hours and usage
below 75% may enable `PRE_RESET_BURN` for explicitly eligible checkpointable work;
urgent work remains ordered first. Explicit reset events are still caller-supplied
and there is no separate reset-event history table.

## Workspace isolation and commit handoff

`GitWorkspaceManager` creates one owned branch and linked worktree for each lease.
Branch/path components are sanitized, Git commands are argument-vector based, and
the base branch is never checked out or mutated by workspace allocation.

Before reusing a `LEASED` workspace, validation checks that:

- the repository and worktree still exist and resolve to the expected repository;
- the path is beneath the configured workspace root;
- the branch and path carry the lease's ownership identity;
- the worktree is registered with Git and is on the expected owned branch.

Validation is read-only. A stale lease is marked failed by the coordinator and a
new lease may be allocated; validation itself never prunes, deletes or repairs
worker data.

Normal release removes only a clean worktree and retains the worker branch and its
tip. Git worktree removal is intentionally not forced. If uncommitted output makes
release unsafe, the persisted lease becomes `RETAINED` for inspection instead of
destroying the output. Agentd never merges a worker branch.

New work starts from the latest resume-capsule commit, otherwise the most recent
dependency result commit, otherwise `HEAD`. On checkpoint, review and completion,
the coordinator fills an omitted result commit from the workspace's current
`HEAD`. This creates a stable handoff reference, but it does not prove that the
worker made a new commit or that review/integration accepted it.

## Interfaces and execution adapters

`ControlPlane` is the transport-independent administrative/application facade. It
submits and inspects work, manages nodes and quota snapshots, exposes lifecycle and
review/repair commands, and delegates effects to the coordinator. `AgentDaemon`
performs managed-driver startup recovery, refreshes Codex account telemetry at a
bounded interval and when new Codex work appears, reconciles live usage and policy
commands, converts valid terminal SDK turns to `REVIEW`, starts pending repairs,
and dispatches ready work.

`AgentAPI` uses the run ID as an opaque bearer capability. Its six worker actions
are `get_assignment`, `request_refinement`, `report_blocker`, `checkpoint`,
`request_review`, and `complete`; `request_history` inspects the retained requests
for that same run. Mutating calls reject stale attempts. The returned
`ExecutionContract` contains the assignment, acceptance criteria, dependency
handoffs, allowed filesystem scope, model class and completion/checkpoint protocol,
but no node ID, quota balance, QoS rank, scarcity or scheduler rationale.
Refinement/blocker requests use a bounded in-memory record because the state-store
port has no generic durable event surface.

`FakeHarnessDriver` is deterministic and performs no harness I/O. It supports
configurable results and call inspection for tests.

The primary `codex` capability is `CodexSdkDriver`, using pinned
`openai-codex==0.144.4` and the SDK-bundled App Server runtime. Production turns
are fixed to `gpt-5.6-terra` with effort `medium`. The adapter starts/resumes
threads, streams turn/token notifications, uses a strict structured review-result
schema, and supports steering, safe-boundary checkpoint/suspend commands,
interrupts, idempotent collection, and same-thread continuation. Its sandbox
request makes only the lease writable, disables network access, and explicitly
restricts read-only roots to the worktree and configured toolchain paths.

The pinned generated schema does not retain the newer `readOnlyAccess` field. The
driver keeps the raw field and fails closed if App Server rejects it; the reviewed
Linux deployment additionally requires an outer Bubblewrap boundary and a startup
canary proving account/state unreadable, worktree writes available, and model
network unavailable. An outer same-UID container mount alone is not treated as
sufficient isolation.

The optional `codex-cli` capability is `CodexCliDriver`. It launches a local,
shell-free `codex exec --json` process and retains the previous process-group
termination behavior, but it does not provide the managed restart or live-token
path. `CodexDriver` remains a Python import alias for this legacy adapter.

The only worker backend is `LocalWorkerBackend`. There is no HTTP/JSON or MCP
transport, web UI, SSH/cloud backend, distributed coordinator or leader election.

## Persistence boundary

SQLite stores jobs, append-only job transitions, workspaces, nodes, allocations,
runs and contracts, checkpoints, quota pools/reservations, append-only usage
samples, managed-driver sessions and latest observations, provider snapshots, and
run commands/acknowledgements. File-backed databases enable foreign keys and WAL
mode. The following multi-record operations are atomic:

- job snapshot plus its transition;
- job transition plus corresponding run snapshot;
- node accounting plus resource allocation/release;
- quota-pool accounting plus reservation/release;
- usage-sample deduplication plus positive-delta reservation/pool charging; and
- compare-and-swap observation-cursor advancement plus the latest observation and,
  for terminal observations, driver-session deactivation.

The process-local lock serializes use of one store instance, while optimistic
snapshot validation catches stale accounting updates. The schema is created in
place and has no versioned migration mechanism. App Server client objects, stream
tasks, legacy CLI/fake process objects, the model API's refinement/blocker records,
and daemon polling timestamps remain memory-only. Durable commands are delivered
at least once: acknowledgement occurs only after the provider call succeeds and
the command ID is included in steering text, but a controller crash between those
two effects can cause replay.
