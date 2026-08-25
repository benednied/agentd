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

For exact environment values and CLI flags, see [configuration](../40-operations/configuration.md)
and the [CLI reference](../80-reference/cli.md).
