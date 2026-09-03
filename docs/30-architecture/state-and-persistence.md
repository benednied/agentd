# State and persistence

SQLite stores jobs, append-only job transitions, workspaces, nodes (including the
latest authenticated worker-heartbeat snapshot), allocations, runs and contracts,
checkpoints, quota pools and reservations, append-only usage samples, managed-driver
sessions and observations, provider snapshots, the append-only artifact ledger,
durable run commands/acknowledgements, Agent Requests, and the exactly-once provider
reset-event ledger.

File-backed databases enable foreign keys, a configured busy timeout, and WAL mode.
The process-local lock serializes one store instance; separate processes rely on
SQLite locking, WAL, and persisted optimistic checks. Only one active scheduling
daemon is supported per database.

The following multi-record operations are atomic:

- a job snapshot and its transition;
- a job transition and corresponding run snapshot;
- node accounting and resource allocation/release;
- quota-pool accounting and reservation/release;
- usage-sample deduplication and positive-delta charging; and
- observation-cursor advancement, latest observation, and terminal session
  deactivation; and
- reset-event identity/application, Agent Request append/update operations, and
  authenticated worker-heartbeat updates on the existing node snapshot.

`SCHEMA_VERSION = 4` is recorded through SQLite `PRAGMA user_version`. Version 0
databases bootstrap the base schema as version 1; the explicit idempotent migration
chain then applies `v1 -> v2` (artifacts and Agent Requests), `v2 -> v3` (the reset
ledger), and `v3 -> v4` (remove the digest-wide artifact uniqueness and enforce
producer-slot ownership). Databases newer than the running binary are rejected.
Future schema changes require another explicit idempotent migration.

Memory-only state includes App Server client objects, stream tasks, fake and CLI
process objects, and daemon polling timestamps. Durable artifacts, Agent Requests,
commands, reset events, and worker-heartbeat snapshots survive restart. Commands
are delivered at least once; a controller crash between provider success and
acknowledgement can replay a command, while the worker-side operation journal
reserves remote requests and fails closed when a side effect was left pending
across worker restart. It also claims each durable run ID before START reaches a
driver, so changing the transport request ID cannot repeat an unknown start in
the same configured worker epoch.

Artifact references are immutable values, but their ledger rows retain provenance:
the same digest reference may have multiple producer rows. The uniqueness rule is
the producer job plus declared output slot; an `ArtifactSelector` always resolves
by producer and slot, never by digest alone.

SQLite transactions do not include Git, Docker, Compose, or other external process
side effects. Without a transactional outbox, a controller crash can leave an
external effect in an orphan gap between that effect and its durable lifecycle
record. The worker journal makes a pending remote request fail closed after a
worker crash; it does not make the external effect and the control-plane commit
atomic.
