# Domain models

This page is the field-level reference for the frozen dataclasses in
`agentd.domain.models`. The serialized field names are the public persistence
format. Enum values are listed in their persisted form. Defaults below describe
the Python constructors; required fields have no default.

## Planning and quota models

### `EffortEstimate`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `p50` | `float` | Median expected effort; must be non-negative. |
| `p90` | `float` | High-percentile effort; must be at least `p50`. |
| `p99` | `float \| None`, `None` | Optional tail estimate; when present it must be at least `p90`. |
| `unit` | `str`, `"agent-minutes"` | Unit for the effort estimates. |

### `QuotaBudget`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `implementation` | `float` | Expected implementation-path quota. |
| `review` | `float`, `0` | Expected review-path quota. |
| `repair` | `float`, `0` | Expected repair-path quota. |
| `validation` | `float`, `0` | Expected validation-path quota. |
| `maximum` | `float \| None`, `None` | Cumulative hard ceiling. If present it must be at least `expected_path`. |
| `pool_id` | `str`, `"default"` | Quota pool from which the reservation is drawn. |
| `unit` | `QuotaUnit`, `QuotaUnit.ABSTRACT` | Dimension of all budget amounts. |

`expected_path` is the derived sum of implementation, review, repair, and
validation. Quota values are finite and non-negative.

### `BurnPolicy`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `eligible` | `bool`, `False` | The job may be considered for pre-reset burn work. |
| `checkpointable` | `bool`, `False` | The job declares a safe checkpoint path for that work. |

### `ResourceVector`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `cpu` | `float`, `1` | CPU capacity units. |
| `ram_gb` | `float`, `1` | Memory capacity in GiB. |
| `gpu_count` | `int`, `0` | Number of GPUs. |
| `vram_gb` | `float`, `0` | GPU memory in GiB. |

All resource values must be non-negative. `fits_within`, addition, and
subtraction operate component-wise.

### `ExecutionRequirements`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `os` | `str \| None`, `None` | Required operating-system label. |
| `arch` | `str \| None`, `None` | Required architecture label. |
| `browser` | `bool`, `False` | Requires browser capability. |
| `desktop` | `bool`, `False` | Requires desktop capability. |
| `labels` | `dict[str, str]`, `{}` | Exact node-label requirements. |

### `Job`

Required fields are `project`, `repository`, `objective`, `quota_budget`, and
`effort`.

| Field | Type / default | Meaning |
| --- | --- | --- |
| `id` | `str`, generated | Stable job identifier. |
| `base_ref` | `str`, `"HEAD"` | Git ref from which the workspace is created; it must not be empty or option-like. |
| `dependencies` | `tuple[str, ...]`, `()` | Jobs that must be completed first. |
| `priority` | `int`, `0` | Scheduler priority. |
| `qos` | `QoSClass`, `NORMAL` | Service class used for ordering and admission. |
| `execution_requirements` | `ExecutionRequirements`, empty | Platform and label constraints. |
| `preferred_harnesses` | `tuple[str, ...]`, `("fake",)` | Harness order preferred by the job. |
| `allowed_harnesses` | `tuple[str, ...]`, `("fake",)` | Harnesses permitted for the job. |
| `preferred_model_class` | `str`, `"standard"` | Preferred model class. |
| `minimum_model_class` | `str`, `"standard"` | Lowest acceptable model class. |
| `preemption_policy` | `PreemptionPolicy`, `CHECKPOINT` | Safe preemption policy. |
| `checkpoint_policy` | `CheckpointPolicy`, `ON_REQUEST` | Checkpoint policy. |
| `acceptance_criteria` | `tuple[str, ...]`, `()` | Conditions for review or acceptance. |
| `required_capabilities` | `frozenset[str]`, empty | Harness/node capabilities required by the job. |
| `resources` | `ResourceVector`, one CPU/one GiB | Requested resources. |
| `burn` | `BurnPolicy`, empty | Pre-reset burn eligibility and checkpointability. |
| `state` | `JobState`, `BACKLOG` | Durable lifecycle state. |
| `gang_id` | `str \| None`, `None` | Readiness group identifier. It is a barrier/readiness concept, not a distributed launch. |
| `reconnaissance_for` | `str \| None`, `None` | Parent job for a bounded reconnaissance child. |
| `selected_harness` | `str \| None`, `None` | Harness selected at dispatch. |
| `selected_model_class` | `str \| None`, `None` | Model class selected at dispatch. |
| `created_at` | `datetime`, current UTC | Creation timestamp. |
| `updated_at` | `datetime`, current UTC | Last snapshot update timestamp. |

The `terminal` property is true only for `COMPLETED`, `FAILED`, or
`CANCELLED`.

## Nodes, allocations, and quotas

### `WorkerNode`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `id` | `str` | Logical node identifier. |
| `labels` | `dict[str, str]` | Node labels used for matching. |
| `capacity` | `ResourceVector` | Total advertised capacity. |
| `harnesses` | `frozenset[str]` | Harnesses available on the node. |
| `allocated` | `ResourceVector`, zero | Current allocations. |
| `capabilities` | `frozenset[str]`, empty | Features such as `browser` or `desktop`. |
| `state` | `NodeState`, `ONLINE` | `ONLINE`, `DRAINING`, or `OFFLINE`. |
| `updated_at` | `datetime`, current UTC | Last node snapshot update. |

`available` is derived as `capacity - allocated`.

### `ResourceAllocation`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `job_id` | `str` | Allocated job. |
| `node_id` | `str` | Logical node holding the allocation. |
| `resources` | `ResourceVector` | Reserved resource vector. |
| `id` | `str`, generated | Allocation identifier. |
| `state` | `AllocationState`, `ACTIVE` | `ACTIVE` or `RELEASED`. |
| `created_at` | `datetime`, current UTC | Allocation timestamp. |
| `released_at` | `datetime \| None`, `None` | Release timestamp. |

### `QuotaPool`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `id` | `str` | Stable pool identifier. |
| `provider` | `str` | Provider or accounting source. |
| `remaining` | `float` | Available provider-reported/accounted capacity. |
| `reserved` | `float`, `0` | Capacity held by active reservations. |
| `debt` | `float`, `0` | Accounted usage not covered by remaining capacity. |
| `unit` | `QuotaUnit`, `ABSTRACT` | Pool accounting dimension. |
| `reset_at` | `datetime \| None`, `None` | Expected reset time. |
| `reset_confidence` | `float`, `0` | Confidence from `0` to `1`. |
| `minimum_interactive_reserve` | `float`, `0` | Capacity protected for interactive work. |
| `mode` | `QuotaMode`, `NORMAL` | Current quota policy mode. |
| `updated_at` | `datetime`, current UTC | Last pool update. |

`dispatchable` is derived as `max(0, remaining - reserved - debt)`.

### `ProviderQuotaSnapshot`

This is an append-only provider observation. It does not convert opaque provider
credits into token or credit balances.

| Field | Type / default | Meaning |
| --- | --- | --- |
| `pool_id` | `str` | Local quota pool receiving the observation. |
| `bucket_id` | `str` | Provider rate-limit bucket. |
| `provider` | `str`, `"openai-codex-chatgpt"` | Provider identity. |
| `primary_used_percent` | `float \| None`, `None` | Primary window utilization, `0`–`100`. |
| `primary_window_minutes` | `int \| None`, `None` | Primary provider window length. |
| `primary_reset_at` | `datetime \| None`, `None` | Primary window reset. |
| `secondary_used_percent` | `float \| None`, `None` | Secondary window utilization, `0`–`100`. |
| `secondary_window_minutes` | `int \| None`, `None` | Secondary provider window length. |
| `secondary_reset_at` | `datetime \| None`, `None` | Secondary window reset. |
| `reached` | `bool`, `False` | Provider reports a reached limit. |
| `credits_exhausted` | `bool \| None`, `None` | Provider reports exhausted credits. |
| `rate_limit_reached_type` | `str \| None`, `None` | Provider limit classification. |
| `plan_type` | `str \| None`, `None` | Provider plan classification. |
| `credits` | `JsonValue`, `None` | Opaque provider credit payload. |
| `rate_limit_reset_credits` | `JsonValue`, `None` | Opaque reset-credit payload. |
| `id` | `str`, generated | Snapshot identifier. |
| `observed_at` | `datetime`, current UTC | Observation timestamp. |
| `confidence` | `float`, `1` | Observation confidence, `0`–`1`. |
| `source` | `str`, `"codex-app-server"` | Observation source. |
| `metadata` | `dict[str, JsonValue]`, `{}` | Additional provider metadata. |

`reset_at` is derived as the earliest non-null primary or secondary reset.

### `QuotaReservation`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `job_id` | `str` | Owning job. |
| `pool_id` | `str` | Source quota pool. |
| `amount` | `float` | Reserved amount. |
| `id` | `str`, generated | Reservation identifier. |
| `state` | `ReservationState`, `ACTIVE` | Reservation lifecycle. |
| `consumed` | `float`, `0` | Settled cumulative usage. |
| `debt` | `float`, `0` | Usage debt attached to the reservation. |
| `unit` | `QuotaUnit`, `ABSTRACT` | Reservation accounting dimension. |
| `created_at` | `datetime`, current UTC | Reservation timestamp. |
| `released_at` | `datetime \| None`, `None` | Release timestamp. |

`outstanding` is derived as `max(0, amount - consumed)`.

## Workspaces, contracts, and runs

### `WorkspaceLease`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `job_id` | `str` | Owning job. |
| `repository` | `str` | Repository path or URL. |
| `branch` | `str` | Isolated Git branch. |
| `working_directory` | `str` | Worktree directory. |
| `base_ref` | `str` | Git base ref. |
| `id` | `str`, generated | Lease identifier. |
| `environment` | `dict[str, str]`, `{}` | Lease-scoped environment. |
| `runtime_namespace` | `str \| None`, `None` | Optional runtime namespace. |
| `container` | `str \| None`, `None` | Optional container identifier. |
| `allocated_ports` | `tuple[int, ...]`, `()` | Ports held by the lease. |
| `temporary_directories` | `tuple[str, ...]`, `()` | Temporary directories held by the lease. |
| `service_namespace` | `str \| None`, `None` | Optional service namespace. |
| `commit` | `str \| None`, `None` | Current accepted/working commit. |
| `state` | `WorkspaceState`, `LEASED` | Lease lifecycle. |
| `created_at` | `datetime`, current UTC | Lease timestamp. |
| `released_at` | `datetime \| None`, `None` | Release timestamp. |

### `ResumeCapsule` and `Checkpoint`

`ResumeCapsule` contains `completed`, `current`, `next_steps`, `known_failures`,
and `decisions` tuples, plus an optional `commit`. It is compact handoff state,
not a copy of the full conversation or workspace.

`Checkpoint` contains `job_id`, `run_id`, a `capsule`, generated `id`, and
`created_at`.

### `ExecutionContract`

The contract is the worker-visible assignment. It deliberately omits quota
balances, node identity, QoS rank, scarcity, and scheduler rationale.

| Field | Type | Meaning |
| --- | --- | --- |
| `job_id` | `str` | Job being executed. |
| `objective` | `str` | Assignment objective. |
| `scope` | `str` | Work scope and boundaries. |
| `acceptance_criteria` | `tuple[str, ...]` | Conditions for completion/review. |
| `dependency_results` | `dict[str, str]` | Results handed off by dependencies. |
| `role` | `str` | Worker role. |
| `allowed_filesystem_scope` | `tuple[str, ...]` | Paths the worker may modify or inspect. |
| `checkpoint_expectations` | `str` | Checkpoint requirements. |
| `coordination_mechanisms` | `tuple[str, ...]` | Allowed coordination channels. |
| `completion_protocol` | `str` | How the worker reports completion. |
| `working_directory` | `str` | Assigned workspace path. |
| `environment` | `dict[str, str]` | Assignment environment. |
| `model_class` | `str` | Selected model class. |
| `resume` | `ResumeCapsule \| None` | Optional resume context. |

### `RunRecord`

| Field | Type / default | Meaning |
| --- | --- | --- |
| `job_id` | `str` | Owning job. |
| `node_id` | `str` | Logical execution node. |
| `workspace_id` | `str` | Workspace lease. |
| `reservation_id` | `str` | Quota reservation. |
| `allocation_id` | `str` | Resource allocation. |
| `driver` | `str` | Harness driver identity. |
| `backend` | `str` | Worker backend identity. |
| `contract` | `ExecutionContract` | Worker-visible assignment. |
| `handle` | `RunHandle` | Driver run identity. |
| `id` | `str`, generated | Durable run identifier. |
| `state` | `RunState`, `STARTING` | Run lifecycle state. |
| `started_at` | `datetime`, current UTC | Start timestamp. |
| `ended_at` | `datetime \| None`, `None` | End timestamp. |
| `result` | `RunResult \| None`, `None` | Terminal result, when available. |

`RunHandle` contains `id`, `driver`, and optional provider `external_id`.

### `RunObservation` and `RunResult`

`RunObservation` is an SDK-neutral telemetry reading with `run_id`, `thread_id`,
`turn_id`, `cursor`, `terminal`, `telemetry_valid`, optional `usage`, optional
`cumulative_quota`, `unit`, `source`, optional `run_state`, optional `result`,
optional `provider_epoch`, `observed_at`, and metadata. A terminal observation
with an invalid or absent normalized cumulative usage cannot be settled as a
trusted usage sample.

`RunResult` contains `outcome`, optional `summary`, optional `commit`,
`consumed_quota`, metadata, and optional `TokenUsage`. `TokenUsage` contains
integer cumulative `input_tokens`, `cached_input_tokens`, `output_tokens`, and
`reasoning_output_tokens` counters.

## Persisted enum values

| Enum | Values |
| --- | --- |
| `QoSClass` | `interactive`, `blocker`, `committed`, `normal`, `speculative`, `scavenger`, `hors-categorie` |
| `JobState` | `BACKLOG`, `PLANNING`, `READY`, `ADMITTED`, `RUNNING`, `DRAINING`, `CHECKPOINTED`, `METERING_PENDING`, `SUSPENDED`, `REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `JobDisposition` | `run`, `throttle`, `suspend`, `resume`, `cancel`, `degrade`, `backlog` |
| `PreemptionPolicy` | `never`, `checkpoint`, `turn-boundary` |
| `CheckpointPolicy` | `none`, `on-request`, `periodic`, `turn-boundary` |
| `QuotaUnit` | `abstract`, `tokens` |
| `QuotaMode` | `NORMAL`, `RESET_ANNOUNCED`, `PRE_RESET_BURN`, `RESET_CONFIRMED`, `EMERGENCY_CONSERVE` |
| `ReservationState` | `ACTIVE`, `METERING_PENDING`, `RELEASED`, `CANCELLED` |
| `WorkspaceState` | `LEASED`, `RETAINED`, `RELEASED`, `FAILED` |
| `NodeState` | `ONLINE`, `DRAINING`, `OFFLINE` |
| `AllocationState` | `ACTIVE`, `RELEASED` |
| `RunState` | `STARTING`, `RUNNING`, `DRAINING`, `CHECKPOINTED`, `SUSPENDED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `TailAction` | `continue`, `reestimate`, `checkpoint-replan`, `convert-to-hors-categorie` |
| `RunOutcome` | `completed`, `failed`, `cancelled` |

The exact job transition graph is in the [state machine reference](state-machine.md).
