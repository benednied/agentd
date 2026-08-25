# Documentation map

This directory is organized by reader intent, not by implementation vocabulary.
The number prefixes provide stable ordering and leave room for future sections;
the unused `60-*` range is intentional.

| Section | Purpose |
| --- | --- |
| `10-start-here` | Onboarding and first contact |
| `20-using-agentd` | Task-oriented product behavior |
| `30-architecture` | Current implementation model and ownership boundaries |
| `40-operations` | Deployment, configuration use, security, and diagnosis |
| `50-development` | Contributor setup, testing, writing, and review |
| `70-decisions` | Durable architectural “why” decisions |
| `80-reference` | Exact, normative commands, models, states, and terms |
| `90-archive` | Historical, non-normative material |

## Documentation rules

- Tutorials and guides explain how to accomplish a task.
- Architecture pages explain how the current system is implemented and where
  ownership lies.
- ADRs preserve why a decision was made. They are retrospective when the decision
  predates this ADR directory and are superseded rather than silently rewritten.
- Reference pages are the normative detail source. Literal defaults, field names,
  enum values, and allowed transitions belong there and are checked where practical.
- Archive pages are not current product or operational guidance.

The root [README](../README.md) is the project landing page. It links into this
map but does not duplicate the normative details.
