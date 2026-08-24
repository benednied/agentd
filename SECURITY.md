# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Send the maintainer a
private report through GitHub's private vulnerability reporting feature. Include
the affected revision, a minimal reproduction, impact, and any suggested
mitigation. Do not include production credentials, Codex authentication material,
worker output, or customer repository contents.

The maintainer will acknowledge a complete report, validate it against a supported
revision, and coordinate disclosure after a fix is available. No response-time or
bounty commitment is currently offered.

## Supported revisions

Until the project publishes versioned releases, only the current `master` branch is
supported. Deployment-specific paths and policies in `deploy/` describe the
reviewed single-host installation; they are not a general multi-tenant security
boundary.

## Security boundaries

`agentd` schedules and supervises coding agents. Treat its SQLite database, Codex
home, workspaces, logs, container host, and deployment configuration as sensitive.
Never place credentials or worker prompts in logs. Keep the documented container
hardening, exact bind mounts, non-root identity, and host access controls intact.

The current MVP does not provide a network API, leader election, or multi-host
coordination. Operators are responsible for database backups, log retention,
credential rotation, host patching, and restricting access to the service account.
