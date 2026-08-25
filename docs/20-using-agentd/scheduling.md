# Scheduling

Scheduling policies are pure and deterministic. They decide whether a job is
ready, how ready jobs are ordered, and which logical node can account for the run.

## Readiness and ordering

- Dependencies block until every referenced job is `COMPLETED`.
- Gang readiness can hold related jobs until every known member is dependency-ready.
- QoS, explicit priority, age, and stable job ID determine ordering.
- Urgent work remains ahead of pre-reset burn work.
- Provider policy can block admission when telemetry is stale or insufficient.

## Placement

Placement filters operating system and architecture, labels, browser/desktop and
generic capabilities, CPU/RAM/GPU/VRAM, allowed harnesses, and exact model-class
strings. Harness preference wins first; normalized resource waste and stable IDs
provide deterministic best-fit selection.

Placement controls admission and accounting. In the current MVP it does not route
the process to another host; `LocalWorkerBackend` executes locally.

## Special cases

Hors-categorie work starts in `PLANNING` and receives a bounded reconnaissance
child. Tail-governor and burn calculations are explicit policy calls, not an
automatic daemon loop. The current backend provides no distributed gang launch or
multi-host barrier.

The [architecture guide](../30-architecture/control-plane.md) explains effect
ordering. Exact field names and enums belong in the [reference](../80-reference/).
