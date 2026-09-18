# ADR 0004: Authorized GitHub coding in one administrative trust domain

- Status: Accepted for bounded implementation; production enablement requires containment
- Decision date: 2026-09-18
- Supersedes: ADR 0003's exclusion of typed remote coding only

## Decision

Issue #60 defines a narrow product boundary. Build an internal GitHub source
adapter and typed remote coding operation using the existing scheduler, account
reservations, worker protocol, and review lifecycle. Do not add a public API,
multi-tenant boundary, arbitrary shell endpoint, or automatic merge. This records
the boundary decision requested by #49, #50 and #51; broader remote-hardening
issues remain rollout prerequisites.

Repository identity includes the immutable GitHub numeric ID as well as its
canonical owner/name. An administrator configures the repository and eligibility
label. Label presence alone does not authorize execution: a trusted local
administrative approval binds the exact title/body fingerprint. This deliberately
avoids assigning authority to issue authors, label names, or prompt instructions.
A source edit revokes approval even if the label remains. Removing eligibility or
closing a queued issue cancels its logical job. Reopening does not revive a
cancelled job. An edited, unstarted job may be explicitly reapproved using its
existing identity. A job that has run is never silently repurposed.

Source observation, approval evidence, and deterministic logical-job linkage live
in the existing SQLite database. Job creation and linkage commit together.
Admission checks the source approval inside the job-state transaction. The daemon
can invoke an injected source reconciler before dispatch; a failed source refresh
blocks dispatch, while existing run reconciliation continues. Known jobs receive
direct source refresh even when bounded polling does not return them. Network
failure is not interpreted as deletion. Active revocation uses the existing
coordinator cancellation/ownership recovery path.

Repository profiles and portable work orders carry exact base/source identities,
profile digests, bounded account budgets, harness and capability requirements.
Preparation and validation come from trusted configuration. Source intent remains
opaque data. Worker-local paths are resolved after a run is claimed.

Coding and publication are separate authorities. The worker needs actual
credential containment, not only filtered environment variables. The trusted
publisher verifies the collected exact commit and process validation evidence and
owns branch/draft-PR credentials. Publication retries do not invoke coding.

## Recovery and compatibility

Schema version 5 adds source and source-event tables without changing existing
jobs. Old application versions reject newer databases; back up the database before
rolling back binaries. Source bodies are retained as task data; operational logs
and qualification evidence should retain fingerprints, not raw prompts.

The first implementation exposes internal Python integration rather than a
network endpoint. Administrative approval is an explicit operation; it is not an
LLM decision. Deployment must configure polling and a contained worker before
claiming the complete unattended path is qualified.
