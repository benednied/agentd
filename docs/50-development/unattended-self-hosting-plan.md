# HP unattended self hosting plan

Audited on 2 October 2026 against GitHub `master` at
`5d3aba3212957b8628a736f12f22eeb854357d9a` and the live HP. Most of the issue,
execution, and publication pipeline already exists upstream. The HP needs an
integrated release, an agentd repository configuration, and additional recovery
and authorization policies before it can operate without an intervening chat
agent.

The target is an HP service that starts at boot, discovers authorized GitHub
issues, prepares the repository, runs an agent, independently builds and tests
the result, repairs failures within an approved budget, and publishes one PR.
This plan treats a validated draft PR as completed delivery. Human acceptance
and merging remain separate decisions. Automatically merging and deploying a
generated change would require an additional integration policy.

## Evidence and current state

| Area | Verified state |
| --- | --- |
| Upstream implementation | PRs [#73](https://github.com/benednied/agentd/pull/73), [#76](https://github.com/benednied/agentd/pull/76), [#82](https://github.com/benednied/agentd/pull/82), and [#84](https://github.com/benednied/agentd/pull/84) are merged. They supply intake, coding, checkpoint recovery, native backlog reconciliation, persistent services, and publication. |
| Real prior qualification | [Issue #74 produced draft PR #75](https://github.com/benednied/agentd/pull/75). The recorded controller ran on macOS and used an HP worker. This proves a supervised slice, rather than continuous operation entirely on the HP. |
| Active HP deployment | `agentd-selfhost.service` is enabled and its container is healthy, using image `365b37b3a7fe43dfd9f41472d2d1a2304cf40a89`. The older Goldenage service also runs. Neither uses the merged coding controller. |
| Persistent coding services | The controller, worker, and publisher units are absent from the installed user units. There is no `coding-current` release link or running coding container. |
| Retained coding configuration | It targets `benednied/goldenage`, selects issues 4, 5, 17, 18, and 19, and sets `publication_enabled=false`. Both quota thresholds are 100%; current code rejects equal thresholds. This configuration cannot be activated unchanged for agentd. |
| Current self hosting proof | A manually submitted agentd documentation job reached `REVIEW`. Its retained edits passed 25 deployment tests. No GitHub intake or PR publication was exercised by that job. |
| Regression evidence from this audit | All **714 tests passed in 52.16 seconds** against an exported copy of current upstream. The initial restricted run blocked local sockets and nested macOS sandboxes; the permitted rerun passed. This is regression evidence, not an unattended host qualification. |

The self hosting branch was created from the stale `f7d4f08` baseline; upstream
already includes the complete coding composition. Integrating upstream is the first task;
deploying the present branch again would retain the manual submission service.

## Components to reuse

The [coding controller](https://github.com/benednied/agentd/blob/5d3aba3212957b8628a736f12f22eeb854357d9a/src/agentd/coding/controller.py)
already connects revision bound intake, fresh quota admission, authenticated
worker execution, collection, and publication. The native backlog reconciler
checks dependency integration and chooses the target branch commit for new work.
The worker creates an isolated lease, enforces a reviewed repository profile,
captures commits outside model authority, and retains checkpoints and usage.

The [publisher](https://github.com/benednied/agentd/blob/5d3aba3212957b8628a736f12f22eeb854357d9a/src/agentd/publication.py)
independently imports and validates the exact result. It owns GitHub push and PR
credentials, reconciles lost push/create responses, and keeps a durable ledger.
Retries of publication do not run the coding agent again. The coding Compose
manifest and three systemd units provide persistent process supervision.

## Implementation order

### 1 Integrate a single release

Start from current upstream and carry forward the isolated HP profile, explicit
Compose startup command, Python pin handling, correct development dependency
group, private runtime cache, and prepared tool invocation fixes from the self
hosting branch. Resolve the different bootstrap paths deliberately: the coding
worker uses `create_verified_coding_sdk`, while the current proof used the local
bootstrap. A fix in one path does not establish that the other path works.

Build the wheel, source distribution, and SHA identified Linux image from this
combined source. Run regression, documentation, static container policy, and
actual sandbox checks on that release. Retain its source SHA, image digest, and
configuration digests as the deployment identity.

### 2 Install the isolated coding services on the HP

Extend `coding-compose.sh`, `coding-release.sh`, the Compose project name, and
all three units to use the self hosting profile. Preserve canonical container
paths required by the sandbox while binding them to separate host directories
under `agentd-selfhost`. The upstream scripts currently hardcode the ordinary
`agentd` host directories and generic coding unit names.

Create an agentd profile for `benednied/agentd`, immutable repository ID
`1328873039`, branch `master`, and a verified base. Configure the controller,
worker, and publisher with matching profile digests, account identity, TLS
identity, and a stable worker epoch. Verify the host's read credential and the
publisher's repository write permissions; retained credential files alone do
not prove those permissions still work.

Run one controller as the account admission authority. The existing Goldenage
and self hosting schedulers must not independently spend against separate local
allowances for the same Codex account. Preserve their histories and outstanding
reservations during consolidation or draining. Give the worker no publication
credential or Docker socket.

### 3 Productize dependency preparation and package validation

Prepare a locked Python 3.12 environment for agentd before each agent receives
its lease. The worker already supports copying a prepared dependency environment;
deployment must supply and version it, with relocated entrypoints and no editable
link back to a preparation checkout. The upstream operator runbook still calls
out unfinished repository preparation.

Use prepared executables directly for lint, formatting, and tests. The HP proof
found that nested `uv run` can hang after its child exits, so it cannot be an
unqualified production validation command. Pin build tooling as well as runtime
dependencies. Independently build wheel and source distribution, install the
wheel into a fresh environment, and smoke test its imports and CLI. Existing CI
runs lint, documentation checks, and tests; it does not build package artifacts.

Put dependency downloads in a trusted, credential free preparation stage and
execute repository build code inside the validation sandbox. Retain the package
digest, result commit, command exits, and logs. A dependency or lockfile change
must select a matching new environment rather than silently reuse an old one.

### 4 Establish authorization for an ongoing queue

An explicitly approved backlog can already run without per-job chat commands.
Both exact and bounded graph grants enumerate approved node revisions; bounded
approval does not authorize future nodes. A label alone also does not authorize
an issue. Therefore neither mode currently supports an indefinite stream of new
issues under one standing policy.

Add a trusted standing policy for the chosen repository and queue. It must bind
the authorized maintainer or authenticated control action, permitted repository,
eligibility conditions, profile, resource limits, and expiry or revocation rules.
The controller should create durable approval for each observed issue revision
that satisfies that policy. Issue text remains task data and cannot choose
credentials, commands, spending limits, or publication authority. Recheck source
eligibility before dispatch, resume, and external effects. Edits, closed issues,
and changed dependencies need explicit policy treatment and regression coverage.

### 5 Make spending and recovery continue without manual top ups

Configure a standing local spending allowance, interactive reserve, per-job
maximum, maximum attempts, and replenishment policy. Always also require fresh
provider quota evidence. The coding controller initializes its local pool from
`maximum_tokens` and preserves counters across restart, but does not pass an
absolute replenishment amount to the daemon. Once this finite pool is consumed,
provider reset alone will not restart the queue.

Reuse the daemon's durable reset event machinery where appropriate, or add
administrative periodic allowance grants with stable event IDs. An approved
local allowance is distinct from provider capacity; percentage telemetry cannot
be converted into an invented token balance. Reboots and repeated observations
must neither grant twice nor lose debt, usage, or reservations. Per-job budget
increases require their own bounded standing policy.

Checkpoint resume already checks fresh authorization and limits attempts. Active
ownership after some worker crashes still requires maintenance proof. Add a
supervised execution boundary with durable process/session identity, stop
acknowledgements, terminal observations, and usage evidence so the controller can
reattach or safely terminate and resume without a chat agent. A timeout or missing
PID must not erase unresolved ownership. Exercise this with worker termination,
controller termination, host reboot, and transport interruption.

### 6 Add bounded repair and explicit delivery status

Today `validation_failed` is retained and subsequent publication fails again.
There is no automatic validation failure to agent repair loop. Add a controller
path that supplies trusted failure diagnostics to a new bounded coding attempt,
then independently validates the new candidate. Account for all attempts against
the same job maximum and cap repeated repairs. Treat transient validation
infrastructure failures separately from failing repository tests.

The publication store immutably binds a job to its result commit. Repairs need
separate immutable candidate records and a selected final candidate; replacing
the existing intent would violate its recovery guarantees. Preserve evidence for
failed candidates and keep one logical job and one intended PR. A candidate that
already caused an external effect needs a separately defined update policy.

The published ledger is already terminal for delivery, while the job remains in
`REVIEW`. Expose a durable delivered outcome and PR URL that survives restart,
separately from human acceptance. If passing GitHub CI is part of delivery,
observe the exact head commit's checks and include bounded CI repair in this
policy. PR creation ambiguity must keep reconciling the deterministic identity;
never resolve it by blindly creating another PR.

### 7 Supervise progress and retain recovery evidence

Enable the controller, worker, and publisher with infinite lifetime and boot
startup. Confirm operation with the Mac disconnected and without an SSH tunnel.
Add liveness and progress monitoring: all three coding containers currently have
healthchecks disabled. Expected waits for quota or dependency integration must
be distinguishable from a stalled poller, dead worker, or publisher failure.

Persist meaningful changes, blocked reasons, attempt counts, and last successful
source/quota/worker observations. Add bounded retries and backoff for network
failures, disk limits, retention cleanup, backups, and restore verification.
Configure notifications for completion or actionable failure. Irreducible
credential, authorization, ownership, or conflicting PR failures should produce
a durable blocked outcome and notification rather than silent inactivity.

### 8 Qualify the complete unattended path

Run the integrated release entirely on the HP for the existing runbook's minimum
24 continuous hours after the final restart. Approve the initial policy once,
then perform no chat-driven submissions, dispatches, quota refills, repairs,
checkpoint promotions, or PR commands during the window. Record the exact
release and configuration used for every test.

| Scenario | Required evidence |
| --- | --- |
| Eligible new issue | Automatic intake, one logical job, repository preparation, coding, independent package build and tests, one PR, delivered outcome. |
| Failing implementation | Trusted diagnostics, bounded repair, new candidate evidence, cumulative accounting, final delivery or explicit exhausted outcome. |
| Pressure and replenishment | Checkpoint and automatic continuation after fresh capacity evidence; one allowance grant per authorized window. |
| Process and host restart | Preserved identities, safe ownership reconciliation, no duplicate agent execution, restored polling and publication without laptop assistance. |
| Lost push or PR response | The existing intended branch or PR is reconciled; coding is not repeated and no duplicate PR appears. |
| Issue edit, closure, or dependency change | Revocation or dependency wait prevents unauthorized dispatch and subsequent external effects. |
| Network, credentials, or disk failure | Bounded recovery when possible, durable actionable outcome otherwise, preserved work and accounting. |

Retain SQLite integrity reports, transitions, usage, approval records, worker
journals, checkpoints, package and bundle digests, real validation logs, and PR
URLs. Automated tests passing and containers reporting healthy do not establish
this gate. Review the complete evidence against the
[upstream qualification runbook](https://github.com/benednied/agentd/blob/5d3aba3212957b8628a736f12f22eeb854357d9a/docs/40-operations/unattended-qualification.md).

## Updating agentd itself

Unattended issue delivery can run from a fixed reviewed daemon release. To also
upgrade agentd after its PRs are merged, add a separate trusted release builder
and supervisor on the HP. It should observe approved merged commits, build and
verify artifacts, drain execution, back up durable state, activate a compatible
release, check readiness, and automatically undrain or roll back the binaries.
The current coding release script expects a prepared release and deliberately
leaves it drained. It supplies neither a release watcher nor automatic readiness
acceptance. Worker agents must not control this privileged release mechanism.

## First implementation milestone

Integrate upstream and the self hosting fixes, install the isolated three-service
profile, prepare agentd's toolchain, and run one approved issue through real
package validation to a draft PR with every process on the HP. This establishes
the deployable baseline. Standing intake, replenishment, repair, crash recovery,
and the unattended qualification then establish the requested continuous system.
