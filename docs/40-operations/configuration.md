# Operational configuration

Configuration has two separate concerns:

1. `ServiceConfig` defines portable paths and policy values for a local control
   plane.
2. The reviewed deployment composition supplies exact host paths, credentials,
   mounts, and production policy at its trust boundary.

The complete list of supported variables, defaults, and validation rules lives in
the normative [configuration reference](../80-reference/configuration.md). This
page explains how operators use those values; it intentionally does not repeat the
reference table.

## Local development

For the fake path, use a project-local database and a workspace root outside the
repository checkout. CLI path flags override the corresponding environment values:

```bash
uv run agentd \
  --db /absolute/path/to/state.sqlite \
  --workspace-root /absolute/path/to/agentd-workspaces \
  init
```

The fake path does not require a Codex home or authenticated provider account.

## Reviewed service deployment

The deployment profile uses dedicated state, workspaces, Codex home, uv cache, and
repository paths. The host-specific values are defined in `deploy/env/` and
validated by the Compose security checks; they are not portable `src/agentd`
defaults.

Trusted `serve` composition enforces the reviewed production model and reasoning
policy. The portable configuration type can still represent non-production values
for tests and local integrations. See [deployment](deployment.md) and
[security](security.md) for the operational boundary.

## Configuration changes

When adding or renaming a supported variable, update the source, the normative
reference, and the documentation check in the same change. Keep literal defaults in
the reference page rather than copying them into operational guides.
