# Quotas and resources

Jobs carry an expected quota budget and, for managed work, a cumulative maximum.
Quota pools record provider, unit, remaining capacity, and an interactive reserve.
Reservations and node allocations are durable and are updated atomically in
SQLite.

Admission reserves the expected accepted-artifact quota path before allocating
node resources. Positive usage deltas charge the reservation and pool once; usage
sample deduplication and accounting are part of the same atomic operation.

When a run finishes with valid terminal telemetry, node capacity is released and
the trusted usage is settled. If terminal telemetry is missing, inconsistent, or
cannot be matched to the durable ledger, capacity is released but the reservation
remains in `METERING_PENDING`. The system does not guess provider charges.

Provider percentages and opaque credits are signals used by admission policy. They
are not converted into an absolute local token balance. The service refreshes
provider telemetry on a bounded interval; stale telemetry restricts admission.

The cumulative maximum is a stopping threshold, not a strict provider spend cap.
Usage arrives in batches. A final provider batch can cross the maximum before the
controller or worker can request cancellation, and already in-flight work can add
usage while cancellation is pending. The ledger charges all trustworthy observed
usage, including excess; it never clips consumption to the configured maximum.
A coding result that is already proven terminal is retained with
`quota_ceiling_exceeded` evidence instead of trying to cancel a completed turn.
Missing terminal proof retains unresolved ownership and accounting.

The #60 qualification observed 65,010 cumulative tokens against a requested
60,000-token maximum. This demonstrates the reporting/stop limitation; it does
not justify promising that the configured maximum bounds actual provider spend.
Account admission and expected-path reservations remain separate controls.

For exact environment values and CLI flags, see [configuration](../40-operations/configuration.md)
and the [CLI reference](../80-reference/cli.md).
