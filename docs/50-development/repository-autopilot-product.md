# Repository autopilot product requirement

Recorded from the owner's request on 4 October 2026.

The user supplies one GitHub repository or several repositories and a protected
Codex reserve. An always-on HP host then turns their backlog into tested draft
pull requests while using the available allowance. It must continue through a
week away without a chat agent periodically SSHing in to restart, submit,
refill, retry, prepare dependencies, or upgrade it.

## Accepted operator policy

- Preserve 10% of the short-term and 10% of the weekly provider window.
- Deliver independently validated PRs for human approval. Checks alone do not
  authorize merging or closing issues. Delivery and acceptance are separate.
- Observe agentd `master` and automatically prepare, qualify, drain, activate,
  and health-check new reviewed releases, retaining durable accounting.
- Run under host supervision across logout, reboot, transient network failures,
  provider resets, and process crashes. Report actionable blockers through the
  existing operations channel.

## Product acceptance criteria

1. Repository onboarding accepts a URL or owner/name list, discovers immutable
   repository identity and dependency requirements, and prepares a tested runtime.
   No repository-specific host patching is part of normal onboarding.
2. Existing eligible issues and newly opened issues are discovered automatically
   under an explicit repository grant. Preserve source provenance and one durable
   job identity per task. Separate umbrella epics and human decisions from work.
3. Multiple repositories share one account quota ledger and a fair scheduler.
   Never create independent allowance pools for the same provider account.
4. Respect issue dependencies, retain progress across quota resets, and advance
   independent issues when one task exhausts its bounded repair allowance.
5. Use the configured provider reserve consistently for new work and running
   work. Pause on unavailable telemetry; resume on fresh capacity. Clearly report
   the limits of observed usage and in-flight overshoot.
6. Keep useful work flowing without an arbitrary daily token limit becoming the
   primary stop condition. Any absolute allowance remains explicit, durable,
   bounded, and renewed by the service rather than by an SSH operator.
7. Survive crash/reboot and a real master upgrade during a week-long unattended
   qualification. Verify no duplicate PRs, erased usage, lost run ownership, or
   automatic acceptance of unreviewed work.

## Current implementation and gaps

The self-hosted release supervisor, durable issue-to-draft pipeline, bounded
repair, renewable local allowance, and GitHub controls exist. The reserve setting
and existing-issue opt-in remove two policy mismatches with this request.

Multiple concurrent repositories remain a product requirement, not a delivered
feature: the coding controller currently selects one repository profile. Target
runtime construction is tracked in issue #92; repository-specific host recovery
is tracked in #90. A successful individual issue or upgrade is not evidence of a
completed week-long soak. Retain these gaps until demonstrated on the HP.
