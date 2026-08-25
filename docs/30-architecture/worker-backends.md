# Worker backends

`WorkerNode` describes logical compatibility and capacity: operating system,
architecture, labels, capabilities, resource vector, allowed harnesses, and model
classes. It is selected by pure placement policy.

`WorkerBackend` describes the mechanism that starts a selected driver. A backend
may eventually execute remotely, but the current implementation provides only
`LocalWorkerBackend`. It validates local compatibility and starts the harness on
the same host as the controller.

Adding a node record does not create a worker process or a network route. It only
adds scheduling and accounting metadata. This separation keeps placement policy
independent from transport and is recorded in [ADR 0003](../70-decisions/0003-worker-node-vs-worker-backend.md).
