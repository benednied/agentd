# Commit messages

Write commit subjects in the imperative mood. Start with a capital letter, do not
end with a period, keep the subject concise, and describe the change rather than
the work performed.

Good examples:

- `Add retry handling for failed imports`
- `Remove deprecated authentication endpoint`
- `Prevent duplicate webhook processing`

Avoid vague subjects such as `Some cleanup`, `Fixes`, or `WIP new retry stuff`.
Issue identifiers may supplement a useful subject but must not replace it.

The commit message should use one project term for one concept. Prefer concrete
verbs such as `add`, `remove`, `check`, `reject`, `retry`, and `validate`. Explain
non-obvious decisions and compatibility constraints in the body; do not narrate
each changed line.
