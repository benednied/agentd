# Why the Goldenage unattended-backlog use case broke down

Incident window: 2026-09-20–21. Analysis recorded: 2026-09-22.
Scope: the dedicated Goldenage coding deployment, not every agentd deployment.

## Expected behavior versus delivered behavior

The user expected to point agentd at a repository, supply a backlog and ordering
policy, and leave HP working until review, a product decision or actual quota
exhaustion required attention. What ran was a supervised issue-to-draft experiment:
a local controller drove a remote coding worker, and a desktop assistant supplied
missing service operations, graph interpretation, recovery diagnosis and budget
replanning. Five drafts were produced, but the control loop was not self-contained.

The core mismatch was the acceptance boundary. Agentd issue60 describes a narrow
single-issue vertical slice. Deployment, source graph derivation, integration
policy, alerts and soak testing were separate backlog items. Success at the end
of one coding/publication transaction was treated too broadly as evidence that an
entire repository backlog could run unattended. The new [roadmap](../ROADMAP.md)
and epic77 make that stronger outcome explicit.

## Evidence and confidence

The operational runbook, controller SQLite status, worker claims/results, process
snapshots, retained checkpoints and contained validation records were used during
supervision. They are retained in the operator's ignored operational state rather
than committed here: they include host-specific configuration and potentially
sensitive execution metadata. This report records safe identifiers and observed
facts; it does not reproduce credentials or raw model transcripts.

Public artifacts: Goldenage [PR25](https://github.com/benednied/goldenage/pull/25)
(CI), [PR26](https://github.com/benednied/goldenage/pull/26) (migration IDs),
[PR27](https://github.com/benednied/goldenage/pull/27) (setup),
[PR28](https://github.com/benednied/goldenage/pull/28) (contribution templates), and
[PR29](https://github.com/benednied/goldenage/pull/29) (upload limits).
Recovery implementation: agentd [PR76](https://github.com/benednied/agentd/pull/76),
stacked on [PR73](https://github.com/benednied/agentd/pull/73). These are implementation
and review artifacts, not proof of merged or released behavior.

## 1. The controller was on the laptop, not on HP

The dedicated coding worker ran in HP's container; intake, scheduling, quota
admission, result collection and trusted publication ran in a Mac process. A local
SSH forward connected them. HP also had an existing production daemon, but that
was a separate process and did not own this Goldenage controller database.

Consequently, a healthy worker did not imply a healthy end-to-end service. A lost
forward prevented collection and publication even when remote coding had finished.
For upload run `d95328f6-7886-44ad-8213-85efe2645fa5`, controller state remained
RUNNING until the tunnel was restored and the same terminal result was collected.
No recoding was necessary. This was control-path unavailability, not model inactivity
inside a still-running job.

The worker itself was launched with a 24-hour lifetime appropriate to qualification.
It later disappeared while idle and required a service restart. Before restarting,
all twelve durable claims were matched to terminal results; there were no unresolved
claims or active coding processes. Restart preserved the node, epoch, databases
and results. A supervised production service should perform this lifecycle without
an assistant launching it through SSH. See #78 and #27.

## 2. Intake was not a complete backlog scheduler

The initial five approved issues were explicitly seeded. A local `backlog.json`
held the twenty-issue graph. At the inspected points, GitHub roadmap22 had no
native sub-issues and issue6 had no native blocked-by edges. Human/assistant
interpretation therefore supplied information the service could not discover from
those fields. An issue poller with deduplication is necessary but insufficient:
it does not automatically define epic membership, topological readiness or what
changes to a graph may inherit execution authorization.

The compiler also used a configured base commit. To advance a real DAG it must
reconcile integrated prerequisites and choose a new exact base containing them;
repeatedly compiling against the initial pinned base is not sufficient. Native
GitHub relationships provide structured data, but agentd still needs pagination,
cycle/missing-node handling, graph snapshots, provenance, authorization and replay
semantics. That is deterministic application work, not a reason to employ an LLM
to parse every issue. See #79/#49/#61 and #80.

## 3. The eventual idle state was an integration wait

All five initial jobs reached REVIEW and published drafts. The remaining coding
candidates depended on CI issue5, represented by unmerged PR25. Issue20 separately
needed a product-language decision. Existing PR23/24 already covered issues3/2.
Starting duplicate jobs or spending more quota would not resolve those conditions.

Human merge control was intentional. The missing product behavior was a durable,
visible explanation of the blocker and automatic progression after valid
integration, not permission to auto-merge. Draft creation, successful validation,
review approval, issue closure and prerequisite integration are distinct facts.
A dependent work order must identify the actual integrated base. Administrative
acceptance also matters: PR25's code and observed quality check passed, but branch
protection/required-check setup and a failing-check merge-block demonstration were
outstanding at the recorded inspection. Code publication alone did not satisfy
all of issue5. See #80/#47.

## 4. Several different limits looked like “Codex ran out”

The run involved independent controls:

| Control | Observed consequence | What a provider reset changes |
| --- | --- | --- |
| Provider allowance/freshness | Admission can wait or provider execution can fail | Fresh observation can establish renewed provider capacity |
| Percentage admission policy | Background admission can stop before true exhaustion | Does not replace explicit policy configuration |
| Local queue reservation pool | New jobs cannot reserve their expected budget | Nothing automatically |
| Cumulative per-job maximum | A continuation can reach the hard limit despite provider capacity | Nothing automatically |
| Effort/runtime governor | A run can be interrupted based on time/estimate | Nothing |

The original 3/5-minute effort estimate caused the first CI run to stop after
about seven minutes. Upload attempts later stopped at 90% of cumulative job caps.
After the user requested disabling percentage stops, a later upload attempt still
hit its absolute 4.5-million-token cap with provider allowance remaining. The local
queue then had 75,905 tokens left, below a 300,000-token expected reservation.
Those were administrative budget stops, not subscription exhaustion.

A freshly observed provider reset later enabled a separately audited local grant
and focused continuation. The reset itself did not replenish those local budgets.
Percentage overrides existed as deployment-only source/config changes, not a
finished released policy interface. Status needs distinct reasons, units, scope
and reset behavior; otherwise a usage bar cannot tell the operator why HP is idle.
See the added acceptance in #63.

## 5. Recovery initially lost the intended checkpoint semantics

The remote interrupt path mapped a checkpoint request to cancellation. Terminal
runs and workspace edits survived, but no usable continuation checkpoint had been
produced automatically. A subsequent attempt exposed an SDK ledger collision:
reusing the logical job identity as the execution-envelope accounting identity
conflicted with the earlier attempt.

PR76 separates stable logical jobs from per-attempt execution identities and adds
trusted checkpoint capture/continuation with identity, bundle and cumulative-usage
validation. Original FAILED/CANCELLED outcomes remain evidence; a continuation is
a new attempt resuming saved edits, not resurrection of the old provider process.
The deployed fixes were qualified with 655 tests and real recovery evidence, but
an open stacked PR and operational deployment are not equivalent to an integrated
release. This distinction must remain in readiness claims. See #30/#63/#41.

## 6. Validation divergence amplified the repair cost

Repeated HP probes hung while isolated Mac full-suite checks completed in roughly
three seconds. The run established that divergence; it did not establish a complete
root cause for every HP probe hang. Treat it as an environment/diagnostic issue,
not proof that tests may be skipped or that the provider was still making progress.

A concrete upload defect was eventually isolated: the code checked
`fastapi.UploadFile`, while parsed form data contained
`starlette.datastructures.UploadFile` instances. Correcting the import in an isolated
contained diagnosis improved the suite from 92 passed/8 failed to 99 passed/1 failed.
The remaining error was onboarding HTML error behavior. A focused continuation fixed
both and passed trusted publication validation. Broad retries before that diagnosis
had consumed substantial cumulative budget with limited test progress.

The final upload attempt used 285,194 tokens, versus 4,813,596 cumulative tokens
across its charged attempts. These are recorded execution-token counters, including
cached input, not a direct conversion to subscription percentage or dollars.
Bounded diagnostics, environment qualification and explicit repair escalation are
needed; raising caps alone is not a recovery strategy. Linux trusted validation must
be qualified when moving publication to HP, rather than copying the Mac sandbox.

## 7. The assistant became the missing operations layer

Desktop heartbeats repeatedly checked processes, tunnels, drafts and quota. That
provided supervision but did not constitute autonomous scheduling. Once the queue
was waiting for integration, repeated unchanged checks could not advance it. The
supervisor should have emphasized the exact integration action rather than leave
the impression that worker health or allowance was the remaining problem.

A running process is not progress. Agentd needs machine-readable execution state,
last-progress timestamps and distinct wait reasons, with deduplicated actionable
notifications. The user should not need a SOTA model to interpret SSH process lists
or decide whether the scheduler has work. See #32 and the #41 qualification profile.

## Corrective plan and non-claims

Deliver #78 persistent services and #79 native graph reconciliation independently;
then #80 connects integration evidence to subsequent admission. Apply #63 policy
and recovery acceptance and #32 state reporting, and qualify the assembled system
through #41 with the laptop unavailable for at least 24 hours. Keep one controller
per database and reconcile ownership before retries. Preserve all evidence and
accounting; retain full contained validation and draft-only publication.

This report creates tracking and explains the failure. It does not migrate the
live controller, merge any PR, authorize new jobs, mark all acceptance complete,
redeem quota credits or claim that the overall backlog has been implemented.
