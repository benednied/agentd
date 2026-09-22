# Roadmap: unattended repository backlog execution

Status: planned, with a demonstrated supervised issue-to-draft foundation.
Updated: 2026-09-22. Owner: unassigned. No delivery dates committed.

The target outcome is: configure a repository, authorized issue/epic selection,
dependency order and execution budgets once; an always-on agentd host then works
through eligible tasks and asks for attention only when a human action is needed.

[Autonomy epic #77](https://github.com/benednied/agentd/issues/77) tracks this
outcome. [The Goldenage technical breakdown](40-operations/goldenage-autonomy-breakdown.md)
explains the evidence and why five successful draft PRs did not establish it.

## Delivery sequence

| Increment | Owner issue | Completion evidence |
| --- | --- | --- |
| Persistent host deployment | [#78](https://github.com/benednied/agentd/issues/78), subset of [#27](https://github.com/benednied/agentd/issues/27) | Controller, worker and trusted publisher operate with laptop off; supervised restart preserves ownership and Linux containment |
| Native backlog discovery | [#79](https://github.com/benednied/agentd/issues/79), implementation under [#49](https://github.com/benednied/agentd/issues/49) | Native sub-issues and blocked-by edges yield a versioned, deterministic DAG with bounded authorization and deduplication |
| Integration-driven progression | [#80](https://github.com/benednied/agentd/issues/80), subset of [#47](https://github.com/benednied/agentd/issues/47) | Proven prerequisite integration selects a valid base and admits children once; review waits identify the required human action |
| Quota and recovery policy | [#63](https://github.com/benednied/agentd/issues/63), [#30](https://github.com/benednied/agentd/issues/30), [PR76](https://github.com/benednied/agentd/pull/76) | Provider and local limits remain distinct; checkpoints retain edits and charges; retries reconcile ownership |
| Actionable state reporting | [#32](https://github.com/benednied/agentd/issues/32) | Execution and every wait reason are observable without a coding model; transition notifications suppress unchanged noise |
| Unattended qualification | [#41](https://github.com/benednied/agentd/issues/41) | At least 24 hours across laptop absence, crashes, lost acknowledgements, quota/reset, validation failure and prerequisite merge |

#78 and #79 can start independently. #80 consumes #79, represented by a native
GitHub blocked-by edge. #78–80 are native sub-issues of #77. The cross-cutting
workstreams above supply scoped acceptance evidence; their entire broader
production scope is not a blanket prerequisite for this increment.

## Existing foundation and limits

[#60](https://github.com/benednied/agentd/issues/60) defines the narrower authorized
issue → remote coding → validated draft PR slice. Its implementation PRs and
[qualification #66](https://github.com/benednied/agentd/issues/66) are evidence for
that slice. They do not establish persistent deployment, DAG scheduling or
sustained unattended operation. Open/stacked PRs and deployment-only patches are
not an integrated release.

The Goldenage initial queue produced drafts25–29. Later issues depend on CI
prerequisite issue5/PR25 integration; issue20 requires a product-language decision.
Existing PR23/24 must be reused rather than duplicated. Draft publication does not
satisfy a dependency. Human review and integration remain intentional gates.

## Release gate and policy

Do not call this roadmap complete until the deployed always-on host passes the
#41 profile and the evidence is linked from #77. Preserve one controller per
database, exact source/base/result provenance, immutable failed attempts,
workspaces/journals and cumulative quota charges. Keep full contained validation
and draft-only publication. No automatic merges or reset-credit redemption.
Provider resets do not silently refill local execution budgets. Optional stacked
PR execution requires an explicit policy, not an implicit workaround.

This scoped roadmap contributes to [#45](https://github.com/benednied/agentd/issues/45);
it does not complete that issue's full production-readiness horizon inventory.
Review this document when a linked issue changes scope, a prerequisite lands,
or new qualification evidence changes the readiness claim. GitHub issues own
implementation detail and live status; this document owns sequence and exit criteria.
