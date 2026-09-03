# Code-review remediation status

This document revalidates the findings in the historical
[`agentd-code-review.md` baseline](https://github.com/benednied/agentd/commit/c9724043e46a)
from commit `c9724043e46a` (2026-08-09). Remediation was committed as `37049aa` on
branch `codex/agentd-control-plane`. It records implemented safeguards and items
deliberately left alone; the historical metrics are not current repository claims.

## Preservation contract

### Logging and sensitive data

- Keep Loguru and centralized sink configuration in `agentd.observability`.
- Preserve structured events at daemon, coordinator, harness, quota, workspace,
  dispatch, repair, reconciliation, and recovery boundaries.
- Keep correlatable `job_id` and `run_id` context, plus relevant resource IDs.
- Never log prompts, objectives, repair instructions, credentials, authentication
  material, raw token values, worker output, exception messages, or tracebacks.
- Keep JSON as the production default and text mode for local diagnosis.

### Durable state and concurrency

- Open SQLite with foreign keys, the configured busy timeout, and WAL for files.
- Treat version 0 to 1 as base-schema bootstrap; the current supported upgrade
  path is the explicit idempotent `v1 -> v2 -> v3 -> v4` chain.
- Increment `SCHEMA_VERSION` only with an explicit idempotent migration from every
  supported prior version; reject databases newer than the binary.
- Keep multi-statement mutations atomic through `_transaction()` and preserve the
  two uniqueness-to-`ConcurrentStateError` paths.
- Preserve append-only transitions, monotonic accounting, retry idempotency, and
  compare-and-swap conflict detection.

### Lifecycle and isolation

- Preserve dispatch compensation order and the `quiesced` rule. Do not release
  allocations, reservations, or workspaces while a process may still run.
- Keep worker-visible `ExecutionContract` data separate from scheduler scarcity,
  quota balances, node identity, QoS rank, and placement reasoning.
- Preserve exclusive Git-worktree checks, path confinement, non-forced cleanup,
  and immutable commit handoff.
- Represent optional lifecycle features with runtime-checkable Protocols rather
  than silent `getattr()` capability discovery.

### Configuration and deployment

- Keep core configuration portable through `HOME` and XDG defaults.
- Enforce reviewed production model and reasoning policy at the trusted runtime
  boundary, not in portable configuration types.
- Keep hardened deployment checks, exact mounts, non-root execution, security
  options, and environment allow-lists synchronized with configuration changes.

### Change discipline

- Do not weaken locked dependency, static type checking, Ruff, formatting, or
  full-test gates.
- Add focused regression tests before changing compensation, recovery, accounting,
  migration, workspace cleanup, or security-boundary code.
- Do not split complex lifecycle functions solely to lower a metric; preserve their
  ordering and crash invariants with direct tests.
- Keep `SECURITY.md` aligned with supported revisions and the actual trust boundary.

## Historical verification baseline

| Check | Recorded result |
| --- | --- |
| Test suite | 286 passed in 12.19 s |
| Statement coverage | 84% |
| Ruff project rules | clean |
| Ruff formatting | clean |
| Public API docstrings | 175/529 (33.1%) |
| Functions above C901 threshold 10 | 11 |

The authoritative commands are:

```bash
uv run ty check
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=agentd --cov-report=term-missing
uv run ruff check src/agentd --select C901
```

## Deferred findings

- Explicit serialization remains framework-independent to avoid migration risk.
- Ruff findings were not suppressed where SQL identifiers are internal and
  subprocesses use argument vectors without a shell.
- `CONTRIBUTING.md`, `CHANGELOG.md`, and `CODEOWNERS` remain deferred until the
  project has multiple maintainers or versioned releases.

Operational log context is narrower than application state. Detailed sensitive
state stays in the access-controlled durable store and workspace, not the log
stream.
