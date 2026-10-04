# Operating agentd through GitHub

The standing GitHub workflow lets an authorized maintainer submit work, provide
feedback, pause execution, and approve integration through issues and pull
requests. Once the controller, worker, and publisher are installed, normal work
does not require a local submission, approval, dispatch, or publication command.
The deployment's trusted policy still fixes the repository, toolchain, validation,
resource limits, and authorized GitHub identities.

This guide describes the implemented controls. A release must also pass the
[unattended qualification](unattended-qualification.md) before its host is claimed
to operate reliably without supervision.

## Submit work

Post a new issue in the configured repository with a clear objective, acceptance
criteria, and any constraints. Issues created by a trusted maintainer after the
policy activation time receive durable approval for their exact title and body.
When the deployment sets `eligibility_label` to `null`, no special label is needed.
An enabled eligibility label remains an additional condition.

By default, existing issues are outside automatic intake. To authorize one,
post a fresh `/agentd approve` comment on that issue after reviewing its current
contents. A comment made before a later title or body edit cannot approve the
changed task. You can bind approval explicitly with
`/agentd approve <source revision>` when the exact revision is available.

The controller prepares the configured repository, waits for fresh provider and
local capacity, and runs a bounded coding attempt. The publisher independently
validates the result and opens a draft PR. The issue's status comment records
progress, waiting reasons, delivery, and integration; the same comment is updated
when the state changes.

An operator can opt the whole existing backlog into the standing policy with
`standing_github_policy.include_existing_issues: true`. Existing issues then use
the same immutable repository, trusted author/editor, eligibility, exact revision,
and deduplication checks as new issues. The activation timestamp still fences
GitHub comments and control commands. This setting does not revive exhausted
attempts, authorize untrusted contributors, or rebind a task that already ran.
Use the native backlog mode when execution requires dependency ordering.

## Protect the interactive reserve

Set `provider_reserve_percent: 10` in the trusted controller and publisher
configuration to protect 10% of both provider windows. This setting overrides the
legacy admission/checkpoint percentages: admission, top-ups, automatic continuation,
health/status reporting, and active-run interruption use the same 90% used boundary.
The most constrained reported window wins. Every reported window must be fresh
and have a percentage. An explicitly null window is absent from that plan, not
unknown usage; a single reported window also requires its duration and reset.
Missing response keys do not establish that a window is absent; missing, future, zero-confidence, or stale telemetry blocks new work and
interrupts active work when an unavailable observation is evaluated. Provider
percentages never become token balances. The independent local allowance and
per-job attempt/token limits still apply.

The reserve is an observed-usage stop threshold, not an exact provider-side cap.
Polling latency, batched usage, in-flight requests, and your other sessions can
consume quota before an interrupt takes effect. No client can guarantee precisely
10% remains. Without this setting, the legacy 75% admission / 90% checkpoint /
98% hard-stop behavior remains. The configurable reserve cannot be below the
existing 2% safety floor.

## Give feedback

An ordinary nonempty comment by a trusted maintainer is additional task intent.
Comments on the issue, PR conversation, review lines, and submitted comment or
change request reviews are supported. For example:

```text
Please cover the empty response case and keep the public function signature.
```

Before execution starts, feedback becomes part of the queued objective. Feedback
that arrives during a run is saved until the run reaches a proven terminal
boundary. A subsequent bounded attempt then addresses it. The remote worker does
not accept arbitrary changes to an in-flight work order.

Feedback on a delivered candidate authorizes an update to its existing branch
and PR. The update compares the old head before writing, so a human change to the
branch is preserved and reported as a conflict. A new candidate requires a fresh
approval before integration. A normal approving review does not become a repair
request merely because it contains a short message.

Each GitHub comment or review ID represents one durable control event. Editing a
previously processed comment does not issue a second command. Post another
comment to change your decision.

## Control a job

Post one command per comment. These commands also work on an agentd PR, where
they control its originating issue.

| Comment | Effect |
| --- | --- |
| `/agentd pause` | Holds admission, automatic continuation, repair, publication, and integration. An active attempt receives a stop request that retains a checkpoint when ownership and usage are proven. |
| `/agentd resume` | Clears the hold and permits continuation under the existing authorization and bounded resource policy. |
| `/agentd cancel` | Cancels the job while preserving unresolved ownership and accounting until the worker confirms its stop. |
| `/agentd retry` | Requests another bounded attempt using the previous task and recorded failures. |
| `/agentd retry <instructions>` | Requests a bounded retry with additional task instructions. |
| `/agentd steer <instructions>` | Supplies the same task feedback as an ordinary trusted comment. |
| `/agentd abandon` | Asks the trusted host supervisor to stop and retire the issue's latest attempt when final usage is missing. It retains unknown accounting and permits fresh independent work within remaining allowance. |

Controls cannot select credentials, shell commands, repository profiles, spending
limits, or an alternate account. A request cannot bypass exhausted cumulative
budget, the attempt cap, invalid telemetry, or unresolved worker ownership.
Refused controls produce an explanation on GitHub.

Abandonment requires the host recovery controls described in
[self-host releases](selfhost-releases.md). It never marks the interrupted job
complete or refunds its reservation. On the operations issue, select the attempt
explicitly with `/agentd abandon <run_id>`. The same command can authorize a
bounded retry of a failed recovery; its original stop proof remains immutable.

An executed task retains its original source revision. Editing its issue title
or body revokes that authority; a new issue is required for a different task.
Use comments for feedback on the existing task. Closing an unmerged issue removes
its execution and integration eligibility. A merge that has already happened is
recorded as history even when GitHub closes the issue before the response reaches
the publisher.

## Approve a pull request

When integration is enabled, approve the current candidate after reviewing its
diff. A trusted native approving review must identify the exact delivered head.
The configured GitHub Actions checks must succeed on that same commit. The
publisher then makes an approved draft ready and merges with an expected-head
check. It never merges merely because the coding agent claims that tests passed.

If the publisher uses your own GitHub account, GitHub treats you as the PR author
and does not allow you to approve it through a native self-review. Use a comment
on that PR instead:

```text
/agentd approve
```

The comment approves the candidate delivered before the comment was posted. To
name the reviewed commit explicitly, use `/agentd approve <full commit SHA>`.
An approval of an earlier candidate does not authorize a later repair result.
The comment grant does not bypass GitHub's own branch protection requirements;
repositories requiring another review still need that review. See
[GitHub's approval rules](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews).

A later trusted change request, pause, pending feedback, changed source, failed
check, changed head, or incompatible base prevents automatic integration. Lost
merge responses are reconciled by reading the exact merged PR rather than
starting another coding run or creating another PR.

## Understand waiting and blocked states

An idle queue, quota wait, dependency wait, or human review wait can be healthy.
The host tracks process liveness separately from admission readiness, so a finite
allowance or provider reset does not masquerade as a crashed service.

Validation failures can cause bounded repairs. Temporary validation, network, or
quota problems retain the job and its evidence for retry. Conflicting human
branch edits, exhausted attempts, revoked authority, invalid usage, and unknown
execution ownership remain blocked until fresh evidence or a permitted GitHub
control resolves them. A retry comment does not invent missing evidence.

Machine status comments are marked and ignored as task feedback, including when
the publisher and maintainer share the same account. The status outbox retains
the intended message before writing. After an ambiguous creation response it
searches for its deterministic marker instead of blindly creating a duplicate.

## Configure the trusted policy

The deployment configures a standing policy with the immutable GitHub repository
ID, a map of trusted login names to numeric user IDs, and an activation timestamp.
GitHub author, body editor, and title edit provenance must agree with that policy
before a new issue revision is approved. A familiar login with a different ID,
an untrusted editor, a recreated repository, or issue text claiming administrative
authority cannot grant approval.

The read process owns intake and control observation. The publisher process owns
GitHub status, branch, PR, and integration writes. Model workers receive neither
publication credentials nor authority to change the standing policy. See
[authorized GitHub coding](github-coding.md) for the execution and publication
boundaries and [deployment](deployment.md) for installation and release recovery.
