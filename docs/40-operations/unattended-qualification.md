# Unattended qualification runbook

This runbook describes a qualification gate; it is not evidence that the
current host has passed. The Linux controller, coding worker, and trusted
publisher must first be deployed from one reviewed release and exercised with
the repository-specific configuration.

The qualification window is 24 continuous hours after the final restart. Start
the window only after the runtime sandbox probe, TLS worker heartbeat, exact
backlog approval, and publication credentials have been checked. Record the
UTC start and end timestamps, release SHA, image digest, controller config
digest, worker session epoch, and database paths.

During the window, use the native backlog flow. Keep the controller and worker
units enabled with infinite process lifetime and let systemd restart only after
a process failure. Do not use qualification lifetime flags in the production
worker. Preserve all databases, journals, workspaces, checkpoints, bundle
manifests, logs, quota observations, and publication evidence until review.

The gate passes only when all of these thresholds hold for the full window:

- zero controller database-lock violations or concurrent controller owners;
- zero unauthorised dispatches, duplicate jobs, duplicate runs, or duplicate
  publication side effects;
- zero worker authentication, TLS, containment, or readiness failures after
  the initial startup checks;
- every admitted run has one durable terminal/review outcome or an explicitly
  retained checkpoint with ownership unresolved and capacity reserved;
- no quota observation is stale or for the wrong account pool;
- every publication attempt has matching source revision, base/result commits,
  validation evidence, and durable reconciliation state; and
- every restart/recovery test preserves job identity, usage accounting,
  checkpoint provenance, and the publisher ledger.

The evidence bundle must include the exact configs and SHA-256 digests, systemd
unit status and journal excerpts, container security validation output, worker
readiness transitions, authenticated heartbeat records, SQLite integrity checks,
quota snapshots, backlog snapshot and approval revision, job/run history,
validation reports, bundle manifests, and publication URLs or blocked reasons.
Include negative evidence for incomplete reads, changed issues, missing worker
heartbeats, stale quota, ambiguous pull requests, and lost publication
responses. Do not claim the gate passed until an operator has reviewed this
bundle against every threshold.
