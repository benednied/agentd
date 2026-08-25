# ADR 0001: Use SQLite for runtime state

- Status: Accepted
- Date: 2026-08-25

## Context

The local MVP needs durable job snapshots, append-only transitions, quota and
resource accounting, managed sessions, usage samples, checkpoints, and commands.
These records must survive daemon restarts and coordinate administrative CLI
processes on one host.

## Decision

Use SQLite as the runtime state store. Enable foreign keys, busy timeout, and WAL
for file-backed databases; keep multi-record state mutations atomic and version the
schema through `PRAGMA user_version`.

## Alternatives

- Keep runtime truth only in memory: rejected because restart recovery and durable
  accounting require persistence.
- Introduce a network database: deferred because the supported MVP is local-first
  and has no distributed coordinator.
- Use an append-only event log as the only representation: rejected for now because
  the control plane needs efficient snapshots and compare-and-swap updates.

## Consequences

The system has a small deployment footprint and strong local transaction semantics.
Only one active scheduler is supported per database; there is no leader election or
general migration chain yet.
