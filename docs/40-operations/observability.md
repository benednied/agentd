# Observability

The service writes structured Loguru events to stderr. Production defaults to
newline-delimited JSON at `INFO`; set `AGENTD_LOG_FORMAT=text` for interactive
diagnosis and use `AGENTD_LOG_LEVEL` for filtering.

Events carry operational identifiers such as `job_id`, `run_id`, `workspace_id`,
`reservation_id`, `pool_id`, and `node_id` where available. The context binder
accepts an explicit allow-list of component, operation, identifiers, driver,
harness, quota unit, outcome, error class, repair-turn, cleanup-error count, and
cancellation fields.

Prompts, objectives, repair instructions, credentials, authentication material,
raw token values, worker output, exception messages, and tracebacks are not logged.
Container or systemd retention and access controls remain part of the operator's
responsibility. Detailed sensitive state stays in the access-controlled durable
store and workspace rather than the log stream.

The daemon, coordinator, harness, quota, workspace, dispatch, repair,
reconciliation, and recovery boundaries should retain correlatable structured
events when changed. See the [development preservation contract](../50-development/review-remediation.md).
