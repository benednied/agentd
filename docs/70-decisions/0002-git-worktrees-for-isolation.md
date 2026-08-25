# ADR 0002: Use Git worktrees for job isolation

- Status: Accepted
- Decision date: Predates ADR repository
- Recorded: 2026-08-25
- Supersedes: None
- Superseded by: None

## Context

Each job needs an exclusive filesystem scope without mutating the caller's base
branch. Results must remain inspectable after failure and be available for a
trusted review handoff.

## Decision

Give every lease an owned Git branch and linked worktree below a configured,
validated workspace root. Revalidate repository identity, branch ownership, Git
registration, lease identity, and path confinement before reuse and handoff.

## Alternatives

- Run directly in the base checkout: rejected because concurrent work could mutate
  caller state and corrupt branch ownership.
- Force-delete temporary worktrees: rejected because dirty output and failed runs
  must remain inspectable.
- Rely only on a container mount: rejected because Git ownership and review-handoff
  invariants still need an application-level lease.

## Consequences

Worker output is isolated and retained. Normal cleanup is non-forced, branches are
not deleted automatically, and repository integration remains outside agentd.
