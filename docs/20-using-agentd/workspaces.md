# Workspaces

Each admitted lease owns one Git branch and linked worktree below the configured
workspace root. The workspace manager validates repository identity, branch
ownership, Git registration, and path confinement before reuse.

The base branch is not mutated during execution. Worker-visible scope contains the
leased worktree only. The worker contract forbids commit, merge, and push operations
for the managed deployment; the trusted coordinator creates a fixed-identity
review-handoff commit after valid managed completion.

Normal release never forces removal. Dirty output is retained for inspection, and
worker branches are not deleted automatically. Lease identity and ownership are
revalidated before cleanup and handoff.

The configured workspace root and the operational security boundary are described
in [configuration](../40-operations/configuration.md) and [security](../40-operations/security.md).
