"""Control-plane domain models.

The repository stores project intent. These models deliberately describe runtime
state only: execution requirements, allocations, runs, and durable checkpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentd.domain.enums import (
    AllocationState,
    CheckpointPolicy,
    JobState,
    NodeState,
    PreemptionPolicy,
    QoSClass,
    QuotaMode,
    ReservationState,
    RunOutcome,
    RunState,
    WorkspaceState,
)

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


def _encode(value: Any) -> JsonValue:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_encode(item) for item in sorted(value, key=str)]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _encode(asdict(value))
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Cannot encode {type(value).__name__} as JSON")


class Serializable:
    def to_dict(self) -> dict[str, JsonValue]:
        encoded = _encode(self)
        if not isinstance(encoded, dict):  # pragma: no cover - defensive invariant
            raise TypeError("A serializable model must encode to an object")
        return encoded


@dataclass(frozen=True, slots=True)
class EffortEstimate(Serializable):
    p50: float
    p90: float
    p99: float | None = None
    unit: str = "agent-minutes"

    def __post_init__(self) -> None:
        if self.p50 < 0 or self.p90 < self.p50:
            raise ValueError("Effort must satisfy 0 <= p50 <= p90")
        if self.p99 is not None and self.p99 < self.p90:
            raise ValueError("Effort p99 must be greater than or equal to p90")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EffortEstimate:
        return cls(
            p50=float(data["p50"]),
            p90=float(data["p90"]),
            p99=float(data["p99"]) if data.get("p99") is not None else None,
            unit=str(data.get("unit", "agent-minutes")),
        )


@dataclass(frozen=True, slots=True)
class QuotaBudget(Serializable):
    implementation: float
    review: float = 0
    repair: float = 0
    validation: float = 0
    maximum: float | None = None
    pool_id: str = "default"

    def __post_init__(self) -> None:
        amounts = (
            self.implementation,
            self.review,
            self.repair,
            self.validation,
        )
        if any(amount < 0 for amount in amounts):
            raise ValueError("Quota budget components cannot be negative")
        if self.maximum is not None and self.maximum < self.expected_path:
            raise ValueError("Quota maximum cannot be below the expected accepted path")

    @property
    def expected_path(self) -> float:
        return self.implementation + self.review + self.repair + self.validation

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaBudget:
        return cls(
            implementation=float(data["implementation"]),
            review=float(data.get("review", 0)),
            repair=float(data.get("repair", 0)),
            validation=float(data.get("validation", 0)),
            maximum=(
                float(data["maximum"]) if data.get("maximum") is not None else None
            ),
            pool_id=str(data.get("pool_id", "default")),
        )


@dataclass(frozen=True, slots=True)
class BurnPolicy(Serializable):
    eligible: bool = False
    checkpointable: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BurnPolicy:
        return cls(
            eligible=bool(data.get("eligible", False)),
            checkpointable=bool(data.get("checkpointable", False)),
        )


@dataclass(frozen=True, slots=True)
class ResourceVector(Serializable):
    cpu: float = 1
    ram_gb: float = 1
    gpu_count: int = 0
    vram_gb: float = 0

    def __post_init__(self) -> None:
        if min(self.cpu, self.ram_gb, self.gpu_count, self.vram_gb) < 0:
            raise ValueError("Resources cannot be negative")

    def fits_within(self, capacity: ResourceVector) -> bool:
        return (
            self.cpu <= capacity.cpu
            and self.ram_gb <= capacity.ram_gb
            and self.gpu_count <= capacity.gpu_count
            and self.vram_gb <= capacity.vram_gb
        )

    def __add__(self, other: ResourceVector) -> ResourceVector:
        return ResourceVector(
            cpu=self.cpu + other.cpu,
            ram_gb=self.ram_gb + other.ram_gb,
            gpu_count=self.gpu_count + other.gpu_count,
            vram_gb=self.vram_gb + other.vram_gb,
        )

    def __sub__(self, other: ResourceVector) -> ResourceVector:
        result = ResourceVector(
            cpu=self.cpu - other.cpu,
            ram_gb=self.ram_gb - other.ram_gb,
            gpu_count=self.gpu_count - other.gpu_count,
            vram_gb=self.vram_gb - other.vram_gb,
        )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceVector:
        return cls(
            cpu=float(data.get("cpu", 1)),
            ram_gb=float(data.get("ram_gb", 1)),
            gpu_count=int(data.get("gpu_count", 0)),
            vram_gb=float(data.get("vram_gb", 0)),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRequirements(Serializable):
    os: str | None = None
    arch: str | None = None
    browser: bool = False
    desktop: bool = False
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRequirements:
        return cls(
            os=str(data["os"]) if data.get("os") is not None else None,
            arch=str(data["arch"]) if data.get("arch") is not None else None,
            browser=bool(data.get("browser", False)),
            desktop=bool(data.get("desktop", False)),
            labels={str(k): str(v) for k, v in data.get("labels", {}).items()},
        )


@dataclass(frozen=True, slots=True)
class Job(Serializable):
    project: str
    repository: str
    objective: str
    quota_budget: QuotaBudget
    effort: EffortEstimate
    id: str = field(default_factory=new_id)
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    qos: QoSClass = QoSClass.NORMAL
    execution_requirements: ExecutionRequirements = field(
        default_factory=ExecutionRequirements
    )
    preferred_harnesses: tuple[str, ...] = ("fake",)
    allowed_harnesses: tuple[str, ...] = ("fake",)
    preferred_model_class: str = "standard"
    minimum_model_class: str = "standard"
    preemption_policy: PreemptionPolicy = PreemptionPolicy.CHECKPOINT
    checkpoint_policy: CheckpointPolicy = CheckpointPolicy.ON_REQUEST
    acceptance_criteria: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    resources: ResourceVector = field(default_factory=ResourceVector)
    burn: BurnPolicy = field(default_factory=BurnPolicy)
    state: JobState = JobState.BACKLOG
    gang_id: str | None = None
    reconnaissance_for: str | None = None
    selected_harness: str | None = None
    selected_model_class: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def terminal(self) -> bool:
        return self.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(
            id=str(data["id"]),
            project=str(data["project"]),
            repository=str(data["repository"]),
            objective=str(data["objective"]),
            dependencies=tuple(str(item) for item in data.get("dependencies", [])),
            priority=int(data.get("priority", 0)),
            qos=QoSClass(data.get("qos", QoSClass.NORMAL)),
            execution_requirements=ExecutionRequirements.from_dict(
                data.get("execution_requirements", {})
            ),
            preferred_harnesses=tuple(
                str(item) for item in data.get("preferred_harnesses", ["fake"])
            ),
            allowed_harnesses=tuple(
                str(item) for item in data.get("allowed_harnesses", ["fake"])
            ),
            preferred_model_class=str(data.get("preferred_model_class", "standard")),
            minimum_model_class=str(data.get("minimum_model_class", "standard")),
            effort=EffortEstimate.from_dict(data["effort"]),
            quota_budget=QuotaBudget.from_dict(data["quota_budget"]),
            preemption_policy=PreemptionPolicy(
                data.get("preemption_policy", PreemptionPolicy.CHECKPOINT)
            ),
            checkpoint_policy=CheckpointPolicy(
                data.get("checkpoint_policy", CheckpointPolicy.ON_REQUEST)
            ),
            acceptance_criteria=tuple(
                str(item) for item in data.get("acceptance_criteria", [])
            ),
            required_capabilities=frozenset(
                str(item) for item in data.get("required_capabilities", [])
            ),
            resources=ResourceVector.from_dict(data.get("resources", {})),
            burn=BurnPolicy.from_dict(data.get("burn", {})),
            state=JobState(data.get("state", JobState.BACKLOG)),
            gang_id=str(data["gang_id"]) if data.get("gang_id") else None,
            reconnaissance_for=(
                str(data["reconnaissance_for"])
                if data.get("reconnaissance_for")
                else None
            ),
            selected_harness=(
                str(data["selected_harness"]) if data.get("selected_harness") else None
            ),
            selected_model_class=(
                str(data["selected_model_class"])
                if data.get("selected_model_class")
                else None
            ),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class WorkerNode(Serializable):
    id: str
    labels: dict[str, str]
    capacity: ResourceVector
    harnesses: frozenset[str]
    allocated: ResourceVector = field(default_factory=lambda: ResourceVector(0, 0))
    capabilities: frozenset[str] = frozenset()
    state: NodeState = NodeState.ONLINE
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def available(self) -> ResourceVector:
        return self.capacity - self.allocated

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerNode:
        return cls(
            id=str(data["id"]),
            labels={str(k): str(v) for k, v in data.get("labels", {}).items()},
            capacity=ResourceVector.from_dict(data["capacity"]),
            allocated=ResourceVector.from_dict(
                data.get("allocated", {"cpu": 0, "ram_gb": 0})
            ),
            harnesses=frozenset(str(item) for item in data.get("harnesses", [])),
            capabilities=frozenset(str(item) for item in data.get("capabilities", [])),
            state=NodeState(data.get("state", NodeState.ONLINE)),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class ResourceAllocation(Serializable):
    job_id: str
    node_id: str
    resources: ResourceVector
    id: str = field(default_factory=new_id)
    state: AllocationState = AllocationState.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceAllocation:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            node_id=str(data["node_id"]),
            resources=ResourceVector.from_dict(data["resources"]),
            state=AllocationState(data.get("state", AllocationState.ACTIVE)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            released_at=(
                datetime.fromisoformat(str(data["released_at"]))
                if data.get("released_at")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class QuotaPool(Serializable):
    id: str
    provider: str
    remaining: float
    reserved: float = 0
    reset_at: datetime | None = None
    reset_confidence: float = 0
    minimum_interactive_reserve: float = 0
    mode: QuotaMode = QuotaMode.NORMAL
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if min(self.remaining, self.reserved, self.minimum_interactive_reserve) < 0:
            raise ValueError("Quota values cannot be negative")
        if not 0 <= self.reset_confidence <= 1:
            raise ValueError("Reset confidence must be between zero and one")

    @property
    def dispatchable(self) -> float:
        return max(0, self.remaining - self.reserved)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaPool:
        return cls(
            id=str(data["id"]),
            provider=str(data["provider"]),
            remaining=float(data["remaining"]),
            reserved=float(data.get("reserved", 0)),
            reset_at=(
                datetime.fromisoformat(str(data["reset_at"]))
                if data.get("reset_at")
                else None
            ),
            reset_confidence=float(data.get("reset_confidence", 0)),
            minimum_interactive_reserve=float(
                data.get("minimum_interactive_reserve", 0)
            ),
            mode=QuotaMode(data.get("mode", QuotaMode.NORMAL)),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class QuotaReservation(Serializable):
    job_id: str
    pool_id: str
    amount: float
    id: str = field(default_factory=new_id)
    state: ReservationState = ReservationState.ACTIVE
    consumed: float = 0
    created_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaReservation:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            pool_id=str(data["pool_id"]),
            amount=float(data["amount"]),
            state=ReservationState(data.get("state", ReservationState.ACTIVE)),
            consumed=float(data.get("consumed", 0)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            released_at=(
                datetime.fromisoformat(str(data["released_at"]))
                if data.get("released_at")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceLease(Serializable):
    job_id: str
    repository: str
    branch: str
    working_directory: str
    base_ref: str
    id: str = field(default_factory=new_id)
    environment: dict[str, str] = field(default_factory=dict)
    runtime_namespace: str | None = None
    container: str | None = None
    allocated_ports: tuple[int, ...] = ()
    temporary_directories: tuple[str, ...] = ()
    service_namespace: str | None = None
    commit: str | None = None
    state: WorkspaceState = WorkspaceState.LEASED
    created_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceLease:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            repository=str(data["repository"]),
            branch=str(data["branch"]),
            working_directory=str(data["working_directory"]),
            base_ref=str(data["base_ref"]),
            environment={
                str(k): str(v) for k, v in data.get("environment", {}).items()
            },
            runtime_namespace=(
                str(data["runtime_namespace"])
                if data.get("runtime_namespace")
                else None
            ),
            container=str(data["container"]) if data.get("container") else None,
            allocated_ports=tuple(
                int(item) for item in data.get("allocated_ports", [])
            ),
            temporary_directories=tuple(
                str(item) for item in data.get("temporary_directories", [])
            ),
            service_namespace=(
                str(data["service_namespace"])
                if data.get("service_namespace")
                else None
            ),
            commit=str(data["commit"]) if data.get("commit") else None,
            state=WorkspaceState(data.get("state", WorkspaceState.LEASED)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            released_at=(
                datetime.fromisoformat(str(data["released_at"]))
                if data.get("released_at")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ResumeCapsule(Serializable):
    completed: tuple[str, ...] = ()
    current: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    commit: str | None = None
    known_failures: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResumeCapsule:
        return cls(
            completed=tuple(str(item) for item in data.get("completed", [])),
            current=tuple(str(item) for item in data.get("current", [])),
            next_steps=tuple(str(item) for item in data.get("next_steps", [])),
            commit=str(data["commit"]) if data.get("commit") else None,
            known_failures=tuple(str(item) for item in data.get("known_failures", [])),
            decisions=tuple(str(item) for item in data.get("decisions", [])),
        )


@dataclass(frozen=True, slots=True)
class Checkpoint(Serializable):
    job_id: str
    run_id: str
    capsule: ResumeCapsule
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            run_id=str(data["run_id"]),
            capsule=ResumeCapsule.from_dict(data["capsule"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class HarnessCapabilities(Serializable):
    name: str
    models: frozenset[str]
    features: frozenset[str]
    native_pause: bool = False
    steering: bool = True
    checkpointing: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionContract(Serializable):
    job_id: str
    objective: str
    scope: str
    acceptance_criteria: tuple[str, ...]
    dependency_results: dict[str, str]
    role: str
    allowed_filesystem_scope: tuple[str, ...]
    checkpoint_expectations: str
    coordination_mechanisms: tuple[str, ...]
    completion_protocol: str
    working_directory: str
    environment: dict[str, str]
    model_class: str
    resume: ResumeCapsule | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionContract:
        return cls(
            job_id=str(data["job_id"]),
            objective=str(data["objective"]),
            scope=str(data["scope"]),
            acceptance_criteria=tuple(
                str(item) for item in data.get("acceptance_criteria", [])
            ),
            dependency_results={
                str(k): str(v) for k, v in data.get("dependency_results", {}).items()
            },
            role=str(data["role"]),
            allowed_filesystem_scope=tuple(
                str(item) for item in data.get("allowed_filesystem_scope", [])
            ),
            checkpoint_expectations=str(data["checkpoint_expectations"]),
            coordination_mechanisms=tuple(
                str(item) for item in data.get("coordination_mechanisms", [])
            ),
            completion_protocol=str(data["completion_protocol"]),
            working_directory=str(data["working_directory"]),
            environment={
                str(k): str(v) for k, v in data.get("environment", {}).items()
            },
            model_class=str(data["model_class"]),
            resume=(
                ResumeCapsule.from_dict(data["resume"]) if data.get("resume") else None
            ),
        )


@dataclass(frozen=True, slots=True)
class RunHandle(Serializable):
    id: str
    driver: str
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunResult(Serializable):
    outcome: RunOutcome
    summary: str = ""
    commit: str | None = None
    consumed_quota: float = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.consumed_quota < 0 or not isfinite(self.consumed_quota):
            raise ValueError("Consumed quota must be finite and non-negative")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        return cls(
            outcome=RunOutcome(data["outcome"]),
            summary=str(data.get("summary", "")),
            commit=str(data["commit"]) if data.get("commit") else None,
            consumed_quota=float(data.get("consumed_quota", 0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class RunRecord(Serializable):
    job_id: str
    node_id: str
    workspace_id: str
    reservation_id: str
    allocation_id: str
    driver: str
    backend: str
    contract: ExecutionContract
    handle: RunHandle
    id: str = field(default_factory=new_id)
    state: RunState = RunState.STARTING
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    result: RunResult | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        handle_data = data["handle"]
        result_data = data.get("result")
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            node_id=str(data["node_id"]),
            workspace_id=str(data["workspace_id"]),
            reservation_id=str(data["reservation_id"]),
            allocation_id=str(data["allocation_id"]),
            driver=str(data["driver"]),
            backend=str(data["backend"]),
            contract=ExecutionContract.from_dict(data["contract"]),
            handle=RunHandle(
                id=str(handle_data["id"]),
                driver=str(handle_data["driver"]),
                external_id=(
                    str(handle_data["external_id"])
                    if handle_data.get("external_id")
                    else None
                ),
            ),
            state=RunState(data.get("state", RunState.STARTING)),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            ended_at=(
                datetime.fromisoformat(str(data["ended_at"]))
                if data.get("ended_at")
                else None
            ),
            result=(RunResult.from_dict(result_data) if result_data else None),
        )


@dataclass(frozen=True, slots=True)
class StateTransition(Serializable):
    job_id: str
    from_state: JobState | None
    to_state: JobState
    reason: str
    id: str = field(default_factory=new_id)
    occurred_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateTransition:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            from_state=(
                JobState(data["from_state"]) if data.get("from_state") else None
            ),
            to_state=JobState(data["to_state"]),
            reason=str(data["reason"]),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"])),
        )


@dataclass(frozen=True, slots=True)
class QuotaResetEvent(Serializable):
    pool_id: str
    mode: QuotaMode
    expected_reset_at: datetime | None = None
    confidence: float = 0
    new_remaining: float | None = None
    source: str = "external-oracle"
