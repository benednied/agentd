# Pull-request writing

Start with the reason for the change. Explain the problem or requirement before
describing the solution, then call out behavior changes, design decisions,
constraints, side effects, compatibility implications, limitations, and follow-up
work.

Use only sections that contain useful information:

```markdown
# Why

Explain the problem or requirement.

# What changed

Describe the solution at a high level.

# Verification

Describe tests and manual checks.

# Risks

Describe compatibility, migration, deployment, security, or performance risks.
```

Keep sentences short and direct. Use active voice where practical, avoid ambiguous
pronouns and unnecessary jargon, and do not claim something is easy or robust
without evidence. A pull request should add information that is not obvious from
the diff rather than restating file operations.

Verification must describe what actually ran. Risks must mention breaking APIs,
schema changes, changed defaults, new dependencies, deployment ordering, rollback
limits, or security effects when applicable. Issue links supplement the explanation
but do not replace it.
