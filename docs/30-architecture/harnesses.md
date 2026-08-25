# Harnesses

The harness boundary receives an `ExecutionContract` containing the objective,
scope, acceptance criteria, dependency handoffs, workspace, model class, and
optional resume capsule. It does not receive quota balances, node identity, QoS
rank, scarcity, or scheduler reasoning.

## Implementations

- `FakeHarnessDriver` is deterministic and performs no harness I/O.
- `CodexSdkDriver` uses the pinned `openai-codex==0.144.4` SDK and App Server.
  It starts or resumes threads, streams observations and token usage, supports
  steering and safe-boundary checkpoint/suspend commands, and supports same-thread
  repair continuation.
- `CodexCliDriver` launches local, shell-free `codex exec --json`. It retains
  process-group termination behavior but has no managed restart or live-token path.
- `CodexDriver` remains a Python import alias for the legacy CLI adapter.

Managed production turns are fixed to model `gpt-5.6-terra` and reasoning effort
`medium` at the trusted runtime boundary. The SDK adapter uses the reviewed
`agentd-workspace` permission profile, the validated lease as its dynamic writable
root, and no access to Codex account state or agentd SQLite state.

Dependency preparation is a separate trusted boundary. It installs the managed
toolchain before the model transport starts; model-side commands remain confined
to the lease and cannot commit, merge, or push. The trusted coordinator creates
the review handoff commit after valid terminal telemetry.
