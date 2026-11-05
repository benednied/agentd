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

## GitHub REST incidents

On the HP, read the controller and publisher journals together.

```bash
journalctl --user -u agentd-selfhost-controller.service \
  -u agentd-selfhost-publisher.service --since '2 hours ago' --no-pager \
  --grep='github_rest_|github_source_|github_poll_incomplete'
```

- `github_rest_backoff_set` records the component and operation that saw the
  limit, the HTTP status, the reported budget and reset, the effective retry
  deadline, and the GitHub request ID when present. `github_rest_deferred`
  records when another process starts honoring the shared deadline.
  `github_rest_resumed` marks its first successful REST response afterward.
- `github_rest_request_failed` separates access, transport, and HTTP failures.
  An identical operation and failure is logged at most once every five minutes
  per process. Enable `DEBUG` for individual request timing and HTTP status.
- `github_rest_request_summary` reports attempts, conditional requests, valid
  `304` responses, and failures by operation roughly every 15 minutes while
  requests are active. Compare the `304` count with total attempts to see how
  often the server could reuse a response. Compare the shared quota readings
  to track actual primary budget use.
- `github_source_poll_completed` records when the controller last completed a
  source sweep, how many source records it reconciled, and its elapsed time.
  `github_poll_incomplete` identifies pagination limits that kept the sweep
  from refreshing source freshness.

Rate-limit timestamps use Unix seconds. REST events include operation names
and safe request metadata. Response bodies, credentials, command output, raw
request URLs, and ETags are excluded. See the [budget diagnostics](troubleshooting.md#github-rest-budget)
for current shared state and cumulative counters that survive restarts.
