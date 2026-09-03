# System overview

`agentd` is a local-first meta-harness. It chooses what may run, selects a logical
node for compatibility and resource accounting, and gives a bounded execution
contract to either a local harness or an authenticated worker. Distributed
execution is deliberately limited to typed artifact Build/Deploy operations.

```text
Repository intent (caller-supplied)
                         |
                         v
             ControlPlane / AgentDaemon
                         |
             pure scheduling policies
          readiness | QoS | quota | placement
                         |
                         v
             SchedulerCoordinator
       SQLite | GitWorkspaceManager | WorkerBackend
              CodexAccountOracle
                         |
             +-----------+------------+
             v                        v
      HarnessDriver             RemoteWorkerBackend
 Fake | Codex SDK/App Server |   HMAC+TLS worker-serve
 codex-cli (local)             typed Build/Deploy only
```

The repository remains the source of truth for intent. SQLite stores a runtime
snapshot of that intent and the control-plane state; changing a job snapshot does
not update plans, issues, or other repository artifacts.

The [control-plane](control-plane.md) owns admission and lifecycle sequencing.
The [state boundary](state-and-persistence.md) explains durable records. The
[worker](worker-backends.md) and [harness](harnesses.md) documents explain the
execution adapters.
