# First real #60 qualification: stopped run and same-run recovery

## Outcome

The first real qualification ended **CANCELLED**, with no verified result commit
and no draft pull request. It demonstrates quota-pressure stopping and recovery
of one durable execution identity across controller and worker restarts. It does
**not** establish successful completion of #66's issue-to-draft-PR path.

The persisted SDK terminal outcome was `CANCELLED`. An earlier interpretation
mistook a terminal observation for successful completion. Terminal means the turn
has ended; the separate outcome field determines whether it completed, failed,
or was cancelled. This record corrects that interpretation.

## Durable identities

| Field | Identity |
| --- | --- |
| Source issue | [benednied/agentd#71](https://github.com/benednied/agentd/issues/71) |
| Source revision | `fad27d8c2d98629a63e98d87fe86dd2d502b8820a8f94ff5c5ded6d81df2001b` |
| Logical job | `github-78f43397ecf806c6f340fb66522e1c21` |
| Execution run | `d0ee6516-7b88-406a-a168-01f21776759d` |
| Repository base commit | `f7d4f08f280080e14c434293c99034ba06216b6f` |
| Initial worker implementation | `509dd775c90d7831cb45efd3b163e0df25a72638` |
| Recovery worker implementation | `b16f6c696e421afe01d1ba8799aedf2778a4cb9d` |
| Controller implementation | `7671eb9a2d86c98524570f38add8286335e642c8` |
| Final job state | `CANCELLED` |
| Result commit / draft PR | None |

The source revision above was independently reread from GitHub and recomputed
using the source adapter; it matched the run's recorded revision and logical job
identity. Worker and controller commit IDs were resolved from the repository.

## Budget and accounting

| Field | Observed or configured value |
| --- | ---: |
| Configured cumulative token maximum | 60,000 |
| Configured runtime limit | 240 seconds |
| Trusted cumulative input tokens | 64,329 |
| Trusted cumulative output tokens | 681 |
| Trusted cumulative total tokens | **65,010** |
| Cached input tokens, already included in input count | 44,288 |
| Usage above configured maximum | 5,010 |
| Final durable usage-ledger charge | **65,010** |

The provider reported token usage in batches. The quota stop occurred while the
model was inspecting the repository, before it produced a change. The final
batch exceeded the configured maximum; the maximum is an observed-usage stop
threshold, not a guarantee that actual provider spend cannot exceed it. Cached
input tokens are a subset of input tokens and must not be added to the total.

The collected result's `consumed_quota` field was `0`, while its trusted
usage evidence and the controller's durable usage ledger recorded `65,010`.
The zero scalar is therefore **not** evidence of zero consumption. Qualification
reporting uses the trusted usage counters and ledger, preserves this discrepancy,
and does not truncate the observed charge to the requested maximum.

## Recovery observations

- Exactly one logical job and one run identity represented this attempt.
- The live controller was restarted while retaining that job and run.
- The worker was subsequently restarted and recovered the same durable run.
- One provider-backed coding execution was launched. Recovery did not start
  another coding execution or replace the run identity.
- Terminal cancellation was retained; recovery did not reinterpret it as
  successful completion or create a replacement implementation.
- No result commit was captured and no branch/PR publication was attempted for this run.

These are failure-path observations from a real provider-backed execution. They
support the bounded-stop and no-duplicate-reexecution invariants. They do not
qualify successful result collection, trusted validation of a changed commit, or
idempotent draft publication for this attempt.

## Remaining qualification

[Issue #72](https://github.com/benednied/agentd/issues/72) narrows the success case
to one short README paragraph. It has not been admitted or executed. The first
local allowance is exhausted; a proposed additional allocation was rejected by
automatic approval review and requires explicit operator approval. No provider
quota reset was requested and no additional capacity was written to the ledger.

A successful second attempt must have its own source/job/run identities. It must
not be represented as a replay of this cancelled attempt. #66 and #60 remain open
until a real verified result is published as a draft PR and its publication replay
is demonstrated. Synthetic failure/idempotency tests do not replace that evidence.

The [structured record](evidence/issue-71-cancelled.json) was extracted from the
trusted controller database. It contains no credentials or raw issue prompt.
