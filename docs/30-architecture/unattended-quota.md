# Unattended account admission

Trusted GitHub intake assigns `QoSClass.SCAVENGER` and a finite cumulative
`QuotaBudget.maximum`. Issue bodies and issue labels cannot select account pools,
priority or QoS. The expected-path reservation includes implementation, review,
repair and validation; the existing cumulative maximum spans attempts.

Scavenger admission always applies the provider gate, for every harness, even
when the optional Codex account policy is disabled. Missing or unknown balances,
zero-confidence observations, future observations, observations older than five
minutes, and observations preceding an elapsed reset leave work queued. A reset
announcement alone does not replenish provider headroom. Fresh provider usage
must be below the background threshold (75% by default).

Provider percentages are not converted into tokens. They gate the existing
local quota pool, whose absolute budget is controller-configured. All workers
using one provider account must reference **one shared pool ID** in the trusted
configuration. The controller's SQLite reservation transaction serializes the
expected-path reservations against this pool. A restart retains the ledger;
retrying a reservation returns the same reservation identity. Reset reconciliation
preserves outstanding reservations. Do not create one pool per worker for a
shared account, or run independent controller databases against that account.

`QuotaManager.wait_reason(job)` and `SchedulerCoordinator.quota_wait_reason(job_id)` expose
current machine-readable reasons reconstructed from persisted state:
`quota_unknown`, `quota_stale`, `quota_provider_pressure`, `quota_insufficient`,
and `quota_unbounded`. `QuotaAdmissionError.reason` carries the same codes.
Reconciliation of an existing reservation remains possible even when fresh
admission is blocked; unknown or stale provider capacity cannot authorize a top-up.

Managed executions reuse the existing durable governor: nonurgent work checkpoints
at 90% provider usage, provider remaining capacity at or below 2% interrupts,
and cumulative job usage checkpoints at 90% of maximum and interrupts after the
configured hard-cap grace. These commands retain run identity and stable command
IDs across controller restarts. Typed coding integrations must route their trusted
observations through the same governor and deliver its checkpoint/interrupt
commands; a successful dispatch gate alone does not implement active-run policing.
Interactive/blocker jobs retain the separately configured account policy and
interactive reserve behavior.
