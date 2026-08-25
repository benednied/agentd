# System overview

`agentd` is a local meta-harness. It chooses what may run, selects a logical node
for compatibility and resource accounting, and gives a bounded execution contract
to a harness. Physical execution is not distributed in the current MVP.

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
                         v
                  HarnessDriver
          Fake | Codex SDK/App Server | codex-cli
```

The repository remains the source of truth for intent. SQLite stores a runtime
snapshot of that intent and the control-plane state; changing a job snapshot does
not update plans, issues, or other repository artifacts.

The [control-plane](control-plane.md) owns admission and lifecycle sequencing.
The [state boundary](state-and-persistence.md) explains durable records. The
[worker](worker-backends.md) and [harness](harnesses.md) documents explain the
execution adapters.
