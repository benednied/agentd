# Worker backends

`WorkerNode` describes logical compatibility and capacity: operating system,
architecture, labels, capabilities, resource vector, allowed harnesses, and model
classes. It is selected by pure placement policy. Registering a node alone never
starts a process or creates a network route.

`WorkerBackend` describes the mechanism that starts a selected driver or typed
operation. `LocalWorkerBackend` validates local compatibility and starts ordinary
harnesses on the controller host. `RemoteWorkerBackend` is node-bound and uses the
authenticated worker protocol; the coordinator selects it only for typed
`BuildImageOperation` and `DeployImageOperation` jobs. It does not provide a
general remote coding-agent escape hatch.

## Remote artifact worker

The remote MVP is intentionally narrow:

- requests are length-bounded canonical JSON envelopes authenticated with an HMAC
  pre-shared key; TLS 1.2 or newer is required by default (plaintext is an
  explicit loopback-only opt-in). TLS handshakes, frame reads, and typed operation
  responses have bounded timeouts, so idle unauthenticated peers cannot retain a
  worker connection slot indefinitely;
- `START`, `STATUS`, `OBSERVE`, `STEER`, `INTERRUPT`, `CANCEL`, `COLLECT`, and
  `HEARTBEAT` are typed protocol actions, with no shell/model escape field;
- the worker journal reserves requests before side effects and replays the exact
  stored response for completed requests. A request left pending by a worker
  crash fails closed on retry rather than running the operation twice. A separate
  durable `(node, epoch, run_id)` claim also prevents a new request ID from
  restarting the same run after a worker-process restart;
- worker-side operation configuration allowlists source repositories, registries,
  and Compose deployment targets. Git uses bare mirrors and detached confined
  worktrees keyed by repository hash and full commit SHA; OCI builds use BuildKit
  `buildx --push`, exact digest verification, and provenance-checked cache entries.
  Git and Docker subprocesses receive a small explicit environment allowlist,
  rather than inheriting arbitrary worker secrets;
- Deploy resolves Compose to JSON before changing state. Every service image must
  be OCI-digest pinned and use an allowlisted repository, including sidecars. The
  requested image service must consume the exact config revision. The Compose file
  comes from a persistent detached worktree at the exact full config commit; both
  validation and `up` use the declared Compose project name. Idempotence binds the
  image, revision, and resolved-config digest. A file- and directory-synced pending
  intent is committed before `up`; the applied state is committed atomically before
  that intent is removed. Recovery either finalizes an already committed desired
  state or restores the exact previous immutable config. An interrupted first
  deployment is returned to no deployment with `compose down` without removing
  volumes. Failed rollback retains the pending marker and fails closed.

Each node uses a distinct PSK, and a worker server accepts only a file-backed
operation journal whose node and session-epoch identity exactly matches the server.
This prevents cross-node request replay or run-claim collisions through a shared
trust or journal configuration.

The `artifact-verification` capability is part of this boundary. The coordinator
will not publish OCI Build output as a verified artifact unless the selected
backend reports that capability in its authenticated heartbeat; controller config
can require capabilities but cannot grant them. The worker rechecks registry
digests and cache provenance before returning output. A verified output is
therefore a validated typed-operation result, not a blanket trust decision for
arbitrary worker data.

The worker is started with `agentd worker-serve`. See the [CLI reference](../80-reference/cli.md)
for flags and [configuration reference](../80-reference/configuration.md) for
the environment variables. The control plane persists remote run identity and
fails closed if the backend or worker disappears. A durable worker run claim in
state `claimed` or `started` survives a worker-process restart; when the
in-memory handle is gone, `STATUS` reports `known=true` and nonterminal
unresolved. The coordinator keeps that same run ID and its admission resources
until an operator reconciles the external side effect. It never starts a new
run for a claimed identity. `known=false` is authoritative only when the worker
has no durable claim for that run (for example, a validation rejection before
the claim), in which case the coordinator may fail the attempt and release its
resources. A transport/backend error remains a reconciliation error and never
falls back to a local harness.

This separation keeps placement policy independent from transport and is recorded
in [ADR 0003](../70-decisions/0003-worker-node-vs-worker-backend.md). The remote
backend is an artifact-worker MVP, not HA orchestration, Kubernetes integration,
or a UI.
