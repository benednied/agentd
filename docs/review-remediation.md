# Code-review remediation status

This document revalidates the findings in `agentd-code-review.md` (baseline commit
`c9724043e46a`, 2026-08-09). The remediation was committed as `37049aa` on branch
`codex/agentd-control-plane`. It records what was implemented and what was
deliberately left alone rather than treating the old measurements as current.

## Preservation contract for future changes

Commit `37049aa` was an operational-readiness change, not merely a cleanup. Future
changes may redesign its implementation, but must preserve the behavior below or
replace it with an explicitly documented, tested equivalent.

### Logging and sensitive data

- Keep Loguru as the application logging system and keep sink configuration
  centralized in `agentd.observability`. Do not introduce direct sinks throughout
  the codebase or silently fall back to the standard-library logging module.
- Preserve structured operational events at daemon, coordinator, harness, quota,
  workspace, dispatch, repair, reconciliation, and recovery boundaries.
- Preserve correlatable `job_id` and `run_id` context wherever those identifiers
  exist. Workspace, reservation, pool, node, driver, and outcome identifiers should
  remain attached at their respective boundaries.
- Never log prompts, objectives, repair instructions, worker output, credentials,
  authentication material, raw token values, exception messages, or tracebacks from
  untrusted upstream processes. New context fields must be deliberately added to
  the allow-list and covered by a non-leakage test.
- Keep JSON as the production default, retain the text mode for local diagnosis,
  and keep deployment validation aware of both logging environment variables.

### Durable state and concurrency

- Do not open SQLite without `foreign_keys`, the configured `busy_timeout`, and WAL
  for file-backed databases.
- Never edit `SCHEMA` without incrementing `SCHEMA_VERSION` and adding an explicit,
  idempotent migration from every supported prior version. Continue rejecting a
  database whose version is newer than the running binary.
- Keep multi-statement state mutations atomic. Use `_transaction()` for ordinary
  transactions. The two explicit transaction blocks intentionally translate
  SQLite uniqueness failures into `ConcurrentStateError`; do not flatten them
  unless that domain behavior remains tested.
- Preserve append-only transitions, quota/accounting monotonicity, retry
  idempotency, and compare-and-swap conflict detection.

### Lifecycle and isolation

- Preserve the dispatch compensation order and the `quiesced` rule: allocations,
  reservations, and workspaces must not be released while a process may still be
  running. Cleanup failures must remain attached to, rather than replace, the
  original failure.
- Keep worker-visible `ExecutionContract` data separated from scheduler scarcity,
  quota balances, node identity, QoS rank, and placement reasoning.
- Preserve exclusive Git-worktree ownership checks, lease identity validation,
  path confinement, non-forced cleanup, and immutable commit handoff behavior.
- Represent optional lifecycle features with runtime-checkable Protocols. Do not
  restore silent `getattr()` capability discovery. The SDK `model_dump` probe is a
  narrow external-payload compatibility exception.

### Configuration and deployment policy

- Keep core configuration portable through `HOME`/XDG defaults. Personal host paths
  may remain in the reviewed single-host deployment profile, but must not return to
  `src/agentd` defaults.
- Keep the configuration type capable of representing non-production model and
  reasoning choices. Enforce the reviewed production model policy at the trusted
  runtime/deployment composition boundary.
- Keep the hardened deployment checks, exact mounts, non-root execution, security
  options, and environment allow-list synchronized with configuration changes.

### Change discipline

- Do not weaken or remove CI gates for locked dependency sync, Ruff lint, Ruff
  formatting, and the full test suite.
- Add focused regression tests before changing compensation, recovery, accounting,
  migration, workspace cleanup, or security-boundary code.
- Do not split the remaining complex lifecycle functions solely to satisfy a
  complexity number. Refactor them only when their ordering and crash invariants
  have direct regression coverage.
- Keep `SECURITY.md` aligned with the actual supported revisions and trust boundary.
- Before merging, run the authoritative commands below. If coverage falls or an
  intentional invariant changes, explain that change in this document or its
  successor rather than silently accepting the regression.

## Current verification baseline

| Check | Current result |
| --- | --- |
| Test suite | 286 passed in 12.19 s |
| Statement coverage | 84% (5,128 statements, 801 missed) |
| Ruff project rules | clean |
| Ruff formatting | clean |
| Source size | 12,334 Python lines |
| Test size | 8,922 Python lines, 208 test functions, 1,143 assertions |
| Public API docstrings | 175/529 (33.1%) |
| Functions above C901 threshold 10 | 11 |

The authoritative commands are:

```console
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=agentd --cov-report=term-missing
uv run ruff check src/agentd --select C901
```

## Finding-by-finding disposition

| Review finding | Status | Current evidence and decision |
| --- | --- | --- |
| 1. No operational logging | **Completed** | Loguru is a runtime dependency. `observability.py` owns the single stderr sink, JSON/text selection, levels, exception-safe sink settings, and an explicit context-field allow-list. Daemon, coordinator, managed harness, quota, workspace, recovery, and service lifecycle events carry operational IDs without prompts, instructions, credentials, token values, worker output, or exception messages. Unit tests cover structured context and rejection of unknown sensitive fields; deployment configuration and retention responsibilities are documented. |
| 2. No CI | **Completed** | `.github/workflows/ci.yml` runs locked dependency sync, Ruff lint, Ruff format check, and the full Pytest suite with read-only repository permissions and a job timeout. |
| 3. No schema versioning or migrations | **Completed for the current schema** | SQLite initialization reads `PRAGMA user_version`, rejects databases newer than the binary, and idempotently upgrades version 0 to version 1 using the existing `IF NOT EXISTS` schema. Tests cover new, reopened, legacy-version, and future-version databases. Future schema changes must add another explicit migration step. |
| 4. Personal paths in core source | **Completed** | `ServiceConfig` derives defaults from `HOME` and XDG state/data/cache locations. There are zero `/home/bened` or `goldenage` references in `src/agentd`. The reviewed single-host paths remain explicit deployment policy in `deploy/` and deployment documentation. |
| 5. Twelve high-complexity functions | **Partially completed; remaining work deliberately deferred** | The 174-line CLI cascade was replaced by focused process, lifecycle, and store handlers; `cli.main` is no longer a C901 finding. Eleven functions remain above 10. Coordinator dispatch/recovery, streamed result collection, usage settlement, and Git rollback encode failure compensation or state-machine invariants; they were not split merely to lower a metric. Logging slightly increased `_dispatch`'s measured branching, but its compensation order and tests remain intact. Future refactors require invariant-specific regression tests first. |
| 6. Optional methods discovered with `getattr()` | **Completed for lifecycle capabilities** | Runtime-checkable Protocols now represent recovery, reconciliation, admission inspection, pending commands, and repair continuation. The sole remaining `getattr()` adapts an external SDK payload's optional `model_dump` serializer; it is compatibility probing, not silent lifecycle capability discovery, so the original finding no longer applies to it. |
| 7. Repeated transaction boilerplate | **Completed** | A `_transaction()` context manager now owns begin/commit/rollback for 19 state-store transaction paths. Two inserts retain explicit handling because they translate SQLite uniqueness failures into domain-specific `ConcurrentStateError`s; all other repeated boilerplate was removed. Atomic accounting, registration, persistence, and usage suites cover the result. |
| 8. No SQLite busy timeout | **Completed** | Every connection sets a configurable 5,000 ms default `PRAGMA busy_timeout`; invalid negative values are rejected and the connection-local value is tested. |
| 9. Sparse public API docstrings | **Improved; exhaustive coverage deferred** | Targeted operational APIs now document the daemon, local runtime, CLI parser, lifecycle protocol, store protocol, quota/resource errors and manager, and durable-state errors. Measured public API coverage is 33.1%, up from the review's 30%. Exhaustively documenting self-explanatory frozen data fields is not required for this remediation. |
| 10. Model value fixed in `ServiceConfig` | **Completed** | The configuration type can represent non-production model and reasoning choices. The fixed production model/effort policy is enforced only when composing the trusted production runtime, with boundary tests. `RunSupervisor` no longer embeds the deployment policy. |
| 11. Handwritten model serialization | **Deferred** | Explicit serialization still keeps the domain layer framework-independent and stable on disk. Replacing it would introduce migration and compatibility risk without addressing an observed defect. |
| 12. Ruff security findings | **Not actionable** | As established by the original review, the SQL fragments use internal identifiers plus parameterized values and subprocesses use argument vectors without a shell. No suppression or behavior change was added merely to hide false positives. |
| 13. Missing governance files | **Partially completed** | `SECURITY.md` now defines private reporting, supported revisions, and the actual security boundary. `CONTRIBUTING.md`, `CHANGELOG.md`, and `CODEOWNERS` remain deferred until the project has multiple maintainers or versioned releases. |

## Logging data policy

Operational log context is intentionally narrower than application state. Allowed
fields are component/operation names, job/run/workspace/reservation/pool/node IDs,
driver or harness names, quota unit, outcome, error class, repair-turn number,
cleanup-error count, and cancellation state. Exception text and tracebacks are not
emitted because upstream process and SDK errors may contain repository paths,
worker responses, or authentication material. Detailed sensitive state remains in
the access-controlled durable store and workspace rather than the log stream.
