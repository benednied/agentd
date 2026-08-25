# State and persistence

SQLite stores jobs, append-only job transitions, workspaces, nodes, allocations,
runs and contracts, checkpoints, quota pools and reservations, append-only usage
samples, managed-driver sessions and observations, provider snapshots, and run
commands/acknowledgements.

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
  deactivation.

`SCHEMA_VERSION = 1` is recorded through SQLite `PRAGMA user_version`. Version 0
databases bootstrap in place; databases newer than the running binary are rejected.
There is no general version-to-version migration chain yet. The first schema
change must add and test an explicit idempotent migration before increasing the
version.

Memory-only state includes App Server client objects, stream tasks, fake and CLI
process objects, refinement/blocker records, and daemon polling timestamps. Durable
commands are delivered at least once; a controller crash between provider success
and acknowledgement can replay a command.
