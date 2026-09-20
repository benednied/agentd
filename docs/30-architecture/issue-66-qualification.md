# Issue #66: real remote qualification

On 2026-09-18, [issue #74](https://github.com/benednied/agentd/issues/74) completed
the real authorized GitHub issue → remote coding worker → trusted validation →
draft PR path. The resulting [draft PR #75](https://github.com/benednied/agentd/pull/75)
remains open for human review. No merge was attempted.

## Successful run

| Evidence | Observed value |
| --- | --- |
| Approved source revision | `ac67ac634f0c818030c233cb5d79a0384388005468a23780452d7af348321ee5` |
| Durable logical job | `github-052911929abebf566a669d7c5d4946ce` |
| Worker run | `46d57b32-3f41-4947-a9d8-aeb90b5f3326` |
| Pinned base | `f7d4f08f280080e14c434293c99034ba06216b6f` |
| Result commit | `6bda19b061b33c2eb6ec383f7a1058ebab8585af` |
| Controller implementation | `ea2ae99f9bbe72c365a502dcb9363af991873c16` |
| Worker implementation | `40b74fd1ea4a6d84f095ed96986f69e424795930` |
| Actual provider usage | 62,439 cumulative tokens |
| Configured stopping/runtime limits | 140,000 tokens / 180 seconds |
| Run lifecycle elapsed time | 29.938 seconds |
| Complete first controller invocation | 46.78 seconds, including publication recovery |
| Final states | Provider/run `COMPLETED`, job `REVIEW`, publication `published` |
| Result | Exactly one draft PR, #75 |

The worker was reached through a dedicated SSH tunnel to the existing reviewed
Linux runtime. The existing pinned provider SDK ran inside its verified
credential/filesystem/network boundary. The model left file edits; trusted Git
capture created the commit and returned bounded authenticated bundle evidence.
The publisher imported and verified that exact commit in its own object cache.

Trusted macOS sandbox processes, not model statements, validated the change:
`git diff --check` returned 0; a separate Python process verified that exactly
`docs/20-using-agentd/README.md` changed and contained the required paragraph.
Both stdout/stderr hashes and the exact result commit are retained in the
[structured evidence](evidence/issue-74-draft.json).

## Real ambiguous publication effects and replay

The qualification adapter deliberately discarded the response **after** a real
successful GitHub branch push. The durable publisher subsequently found the
intended branch at the recorded result commit. It then discarded the response
**after** real draft creation. The next reconciliation discovered the existing
intended draft instead of creating another one.

A separate fresh controller invocation repeated intake and publication after
success. It returned the same PR in 3.62 seconds. A direct GitHub lookup found
exactly one draft at the recorded head/base. A direct SSH inspection found one
SDK run/session for this issue, already inactive. Coding did not run again during
either injected failure or the controller replay. The controller contains three
historical fixture jobs; this successful issue has exactly one logical job and
one execution run.

## Admission and authority

The initial default policy correctly held #74 in `READY` when fresh quota reached
76% used, above the normal 75% background threshold. The operator explicitly
approved further token spending for this supervised qualification. A trusted
local controller setting selected an 85% threshold, retaining 15% headroom; fresh
provider evidence was still required and the 140,000-token/180-second bounds
remained enforced. The normal 75% default was not changed. Issue text did not
select the threshold, profile, worker, credentials, QoS, or publication policy.

The additional finite local allowance was applied once with a durable grant ID.
Prior usage/debt was retained. No provider reset credit was consumed. The actual
62,439-token completion was charged once, leaving local remaining quota 82,571,
reserved quota 0, and the first attempt's debt 5,010. Provider account capacity was
not inferred from the local allowance. This proves the configured supervised
slice; it is not a claim that a default-policy run can ignore provider pressure.

## Earlier failure fixtures and fixes

- [Issue #71](https://github.com/benednied/agentd/issues/71) quota-cancelled during
  inspection: 65,010 actual tokens against a 60,000 stopping threshold. One job
  and run survived real controller and worker restarts without another provider
  start. No verified result commit/PR was produced. An early interpretation of
  a terminal flag as successful coding was corrected by the persisted cancelled
  outcome. Full identities, admission snapshot, reservation, and charges are in
  [the cancelled-run record](evidence/issue-71-cancelled.json).
- [Issue #72](https://github.com/benednied/agentd/issues/72) exposed a sequential
  worker setup bug before any provider call: recreating the shared node with a
  new timestamp conflicted with its durable identity. Setup is now atomic and
  reuses the node; failures roll back the entire new envelope. For the legacy
  claim, an explicit trusted maintenance operation proved absence of an SDK run
  and session, checked retained job/workspace identities, and recorded a
  zero-token failure. It neither restarted the claim nor changed its identity.
  The normal controller then released its reservation. See the
  [pre-provider failure record](evidence/issue-72-preparation-failed.json).

Other real-path fixes prevent charging identical cumulative usage on a new
progress cursor, collect durable terminal status before requesting a vanished
live handle, and charge final SDK token usage when its generic quota scalar is
zero after restart. Git handoff and validation protect against malicious Git
configuration/hooks/index state. Unknown provider ownership still remains held;
absence of a live process alone never authorizes a retry.

## Scenario coverage

The integrated code passed **637 tests in 39.91 seconds** after the sequential
worker fix. Ruff lint/format and documentation checks pass. The evidence below
separates actual remote-provider behavior from controlled-provider tests.

| Qualification scenario | Evidence |
| --- | --- |
| Real issue → remote coding → draft | #74 → #75, exact identities and process validation above |
| Repeated intake → one job | Real controller replay; `test_github_intake.py` repeated-poll/restart cases |
| Insufficient/stale/unknown quota → wait | Real default-policy pressure wait; `test_unattended_quota.py` and `test_unattended_admission.py` |
| No compatible idle worker → wait | `test_github_coding_pipeline.py` selection checks |
| Lost worker ACK, reconnect, controller/worker restart | `test_coding_recovery.py`, `test_coding_worker.py`; real #71 process restarts |
| Mid-run pressure and bounded stop | Real #71; coding recovery/worker cancellation tests |
| Publication failure without recoding | Real lost push/create responses and fresh controller replay; publication tests |
| Closed/ineligible source before launch | `test_github_intake.py` closure/removal/revision authorization cases |
| Validation failure remains diagnosable | `test_publication.py` persisted failed evidence and refused publication |
| Containment, portable work orders, retention | Actual sandbox probes; Git configuration attack, two-worker-root, and release tests |

## Actual cleanup and retained evidence

After publication and replay, the dedicated test worker and SSH tunnel were
stopped. The successful run's retention operation was called twice over SSH.
Its isolated worktrees/mirrors and transfer bundle were removed; its immutable
result, claim, and worker journal were verified unchanged and retained. The
controller's imported result ref and GitHub draft remain available. Cleanup
started no provider calls and did not modify the production service.

## Review and operational boundary

### Operational hardening, 2026-09-20

The integrated suite now passes **649 tests**. The installed `agentd github`
commands support administrative approval, continuous discovery/execution/publication,
and durable status inspection. New coverage exercises this actual controller
composition over authenticated TCP and real Git, including lost push/create
responses and restart with one execution, one draft, and unchanged charged quota.
The test provider and GitHub adapter are controlled fixtures; this is additional
operational regression coverage, not a claim of another real provider run.

New regressions verify fresh quota polling for newly ingested remote coding and
live source authorization after validation (including closure, edits, eligibility
removal, and identity replacement). Approval/status work without a running worker.
Restart registration preserves remaining, reserved, and debt balances. Lint,
formatting, documentation checks, and targeted module typing pass.

The current macOS validation probe and the already deployed HP Linux validation
probe both passed their synthetic credential, filesystem, Git metadata, and network
checks. The HP container was healthy during inspection. The successful real #74
result remains draft #75 at the recorded commit with passing CI; no additional
provider work or production service restart was required for these checks.

Implementation remains in review PRs #67–#70 and #73. The generated draft #75 is
an output artifact, not permission to merge. The completed slice does not claim
public API/HA/multi-tenant readiness, or complete #23's unrelated driver matrix.
Deployment remains limited to the reviewed containment adapter.

See the [operator guide](../40-operations/github-coding.md) for setup and recovery.
Keep durable journals and claim/result evidence according to retention policy;
do not erase them to make unresolved ownership appear idle. The token maximum is
an observed-usage stopping threshold: a provider batch can overshoot it, as #71
showed. All actual consumption is retained rather than truncated to the threshold.
