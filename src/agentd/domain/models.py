"""Control-plane domain models.

The repository stores project intent. These models deliberately describe runtime
state only: execution requirements, allocations, runs, and durable checkpoints.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from agentd.domain.enums import (
    AgentRequestKind,
    AllocationState,
    ArtifactKind,
    CheckpointPolicy,
    JobState,
    NodeState,
    OperationKind,
    PreemptionPolicy,
    QoSClass,
    QuotaMode,
    QuotaUnit,
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


_GIT_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OCI_IMAGE_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}\Z"
)
_OCI_REPOSITORY_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z"
)
_STABLE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*\Z")


def _require_stable_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty stable name")
    if _STABLE_NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must contain only lowercase name characters")
    return value


def _require_relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty relative path")
    if (
        "\0" in value
        or "\\" in value
        or value.startswith("/")
        or value.startswith("-")
        or any(part == ".." for part in value.split("/"))
        or any(char.isspace() or char in ";|&$`<>" for char in value)
    ):
        raise ValueError(f"{label} must be a confined relative path")
    return value


def _require_registry_repository(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _OCI_REPOSITORY_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "registry_repository must be a lowercase registry/repository without "
            "a tag or digest"
        )
    return value


def _require_source_repository(value: str) -> str:
    """Validate repository identity without accepting shell or URL credentials."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("source_repository must be a non-empty URL or path")
    if (
        "\0" in value
        or value.startswith("-")
        or any(char.isspace() or char in ";|&$`<>" for char in value)
    ):
        raise ValueError("source_repository must not be option-like or shell-like")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "file"}:
            raise ValueError("source_repository URL must use HTTPS or a local file URL")
        if parsed.scheme == "https" and not parsed.hostname:
            raise ValueError("source_repository HTTPS URL must include a host")
        if parsed.scheme == "file" and not parsed.path:
            raise ValueError("source_repository file URL must include a path")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source_repository URL cannot embed credentials")
    elif re.match(r"^[^/]+@[^/]+:", value):
        raise ValueError("source_repository cannot use an SSH/scp-style transport")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactRef(Serializable):
    """A canonical immutable Git commit or OCI image reference."""

    kind: ArtifactKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("Artifact kind must be an ArtifactKind")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("Artifact reference must be a non-empty string")
        if self.value != self.value.strip() or self.value != self.value.lower():
            raise ValueError("Artifact reference must be lowercase and untrimmed")
        if "\0" in self.value:
            raise ValueError("Artifact reference cannot contain NUL")
        pattern = (
            _GIT_COMMIT_RE if self.kind is ArtifactKind.GIT_COMMIT else _OCI_IMAGE_RE
        )
        if pattern.fullmatch(self.value) is None:
            expected = (
                "a complete lowercase 40- or 64-character Git SHA"
                if self.kind is ArtifactKind.GIT_COMMIT
                else "a canonical lowercase registry/repository@sha256:<digest>"
            )
            raise ValueError(f"Artifact reference must be {expected}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactRef:
        return cls(
            kind=ArtifactKind(data["kind"]),
            value=str(data["value"]),
        )


@dataclass(frozen=True, slots=True)
class ArtifactSelector(Serializable):
    """An unresolved dependency binding for a declared producer output."""

    producer_job_id: str
    spec_name: str
    kind: ArtifactKind

    def __post_init__(self) -> None:
        if not self.producer_job_id.strip():
            raise ValueError("Artifact selector requires a producer job")
        _require_stable_name(self.spec_name, "Artifact selector spec name")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("Artifact selector kind must be an ArtifactKind")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactSelector:
        return cls(
            producer_job_id=str(data["producer_job_id"]),
            spec_name=str(data["spec_name"]),
            kind=ArtifactKind(data["kind"]),
        )


ArtifactInput = ArtifactRef | ArtifactSelector


def artifact_input_from_dict(data: dict[str, Any]) -> ArtifactInput:
    """Decode a concrete ref or an explicitly unresolved selector."""

    if not isinstance(data, dict):
        raise TypeError("Artifact input must be an object")
    if "value" in data:
        return ArtifactRef.from_dict(data)
    if "producer_job_id" in data:
        return ArtifactSelector.from_dict(data)
    raise ValueError("Artifact input must be a ref or selector")


@dataclass(frozen=True, slots=True)
class ArtifactSpec(Serializable):
    """A stable output slot with a required artifact family."""

    name: str
    kind: ArtifactKind
    media_type: str | None = None

    def __post_init__(self) -> None:
        _require_stable_name(self.name, "Artifact spec name")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("Artifact spec kind must be an ArtifactKind")
        if self.media_type is not None and (
            not self.media_type
            or self.media_type != self.media_type.strip()
            or any(char.isspace() or char in ";|&$`<>" for char in self.media_type)
        ):
            raise ValueError("Artifact media type must be a compact token")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactSpec:
        return cls(
            name=str(data["name"]),
            kind=ArtifactKind(data["kind"]),
            media_type=(
                str(data["media_type"]) if data.get("media_type") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class BuildImageOperation(Serializable):
    """Typed image-build intent; it contains no shell command fields."""

    source_input: ArtifactInput
    output_name: str
    registry_repository: str
    context: str = "."
    dockerfile: str = "Dockerfile"
    platforms: tuple[str, ...] = ()
    source_repository: str = ""
    kind: OperationKind = field(default=OperationKind.BUILD_IMAGE, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_input, (ArtifactRef, ArtifactSelector)):
            raise TypeError("Build source_input must be an artifact input")
        if self.source_input.kind is not ArtifactKind.GIT_COMMIT:
            raise ValueError("Build source_input must reference a Git commit")
        _require_source_repository(self.source_repository)
        _require_stable_name(self.output_name, "Build output name")
        _require_registry_repository(self.registry_repository)
        _require_relative_path(self.context, "Build context")
        _require_relative_path(self.dockerfile, "Build Dockerfile")
        if any(
            not isinstance(platform, str) or _PLATFORM_RE.fullmatch(platform) is None
            for platform in self.platforms
        ):
            raise ValueError("Build platforms must use lowercase os/architecture names")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BuildImageOperation:
        if data.get("kind") != OperationKind.BUILD_IMAGE.value:
            raise ValueError("Build operation has an invalid kind discriminator")
        return cls(
            source_input=artifact_input_from_dict(data["source_input"]),
            output_name=str(data["output_name"]),
            registry_repository=str(data["registry_repository"]),
            context=str(data.get("context", ".")),
            dockerfile=str(data.get("dockerfile", "Dockerfile")),
            platforms=tuple(str(item) for item in data.get("platforms", [])),
            source_repository=str(data["source_repository"]),
        )


@dataclass(frozen=True, slots=True)
class DeployImageOperation(Serializable):
    """Typed digest-pinned deployment intent; it contains no shell fields."""

    image_input: ArtifactInput
    target: str
    config_revision: ArtifactRef
    deployment_name: str
    kind: OperationKind = field(default=OperationKind.DEPLOY_IMAGE, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.image_input, (ArtifactRef, ArtifactSelector)):
            raise TypeError("Deploy image_input must be an artifact input")
        if self.image_input.kind is not ArtifactKind.OCI_IMAGE:
            raise ValueError("Deploy image_input must reference an OCI image digest")
        if not isinstance(self.config_revision, ArtifactRef):
            raise TypeError("Deploy config_revision must be an ArtifactRef")
        if self.config_revision.kind is not ArtifactKind.GIT_COMMIT:
            raise ValueError("Deploy config_revision must reference a Git commit")
        _require_stable_name(self.target, "Deploy target")
        _require_stable_name(self.deployment_name, "Deploy deployment name")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeployImageOperation:
        if data.get("kind") != OperationKind.DEPLOY_IMAGE.value:
            raise ValueError("Deploy operation has an invalid kind discriminator")
        return cls(
            image_input=artifact_input_from_dict(data["image_input"]),
            target=str(data["target"]),
            config_revision=ArtifactRef.from_dict(data["config_revision"]),
            deployment_name=str(data["deployment_name"]),
        )


JobOperation = BuildImageOperation | DeployImageOperation


def operation_from_dict(data: dict[str, Any]) -> JobOperation:
    """Decode a typed operation without accepting an untagged payload."""

    if not isinstance(data, dict):
        raise TypeError("A job operation must be an object")
    kind = data.get("kind")
    if kind == OperationKind.BUILD_IMAGE.value:
        return BuildImageOperation.from_dict(data)
    if kind == OperationKind.DEPLOY_IMAGE.value:
        return DeployImageOperation.from_dict(data)
    raise ValueError("A job operation requires a known kind discriminator")


@dataclass(frozen=True, slots=True)
class ArtifactRecord(Serializable):
    """Append-only ledger entry for a produced or verified immutable artifact."""

    ref: ArtifactRef
    producer_job_id: str | None
    producer_run_id: str | None
    spec_name: str
    verified: bool = False
    verified_at: datetime | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    external: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError("Artifact record ref must be an ArtifactRef")
        if self.external:
            if self.producer_job_id is not None or self.producer_run_id is not None:
                raise ValueError("External artifacts cannot have producer identifiers")
            if not self.verified or self.verified_at is None:
                raise ValueError("External artifacts must be verified at registration")
        elif (
            not isinstance(self.producer_job_id, str)
            or not self.producer_job_id.strip()
            or not isinstance(self.producer_run_id, str)
            or not self.producer_run_id.strip()
        ):
            raise ValueError("A produced artifact requires producer identifiers")
        if not isinstance(self.external, bool):
            raise TypeError("Artifact external must be a bool")
        _require_stable_name(self.spec_name, "Artifact spec name")
        if not isinstance(self.verified, bool):
            raise TypeError("Artifact verified must be a bool")
        if self.verified and self.verified_at is None:
            raise ValueError("A verified artifact requires verified_at")
        if not self.verified and self.verified_at is not None:
            raise ValueError("An unverified artifact cannot have verified_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactRecord:
        verified_at = data.get("verified_at")
        return cls(
            id=str(data["id"]),
            ref=ArtifactRef.from_dict(data["ref"]),
            producer_job_id=(
                str(data["producer_job_id"])
                if data.get("producer_job_id") is not None
                else None
            ),
            producer_run_id=(
                str(data["producer_run_id"])
                if data.get("producer_run_id") is not None
                else None
            ),
            spec_name=str(data["spec_name"]),
            verified=bool(data.get("verified", False)),
            verified_at=(
                datetime.fromisoformat(str(verified_at))
                if verified_at is not None
                else None
            ),
            metadata=dict(data.get("metadata", {})),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            external=bool(data.get("external", False)),
        )


@dataclass(frozen=True, slots=True)
class EffortEstimate(Serializable):
    p50: float
    p90: float
    p99: float | None = None
    unit: str = "agent-minutes"

    def __post_init__(self) -> None:
        values = (self.p50, self.p90, self.p99)
        if any(value is not None and not isfinite(value) for value in values):
            raise ValueError("Effort estimates must be finite")
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
    unit: QuotaUnit = QuotaUnit.ABSTRACT

    def __post_init__(self) -> None:
        amounts = (
            self.implementation,
            self.review,
            self.repair,
            self.validation,
        )
        if any(not isfinite(amount) or amount < 0 for amount in amounts):
            raise ValueError("Quota budget components must be finite and non-negative")
        if self.maximum is not None and not isfinite(self.maximum):
            raise ValueError("Quota maximum must be finite")
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
            unit=QuotaUnit(data.get("unit", QuotaUnit.ABSTRACT)),
        )


@dataclass(frozen=True, slots=True)
class TokenUsage(Serializable):
    """Cumulative token counters reported by a harness or provider.

    Cached input and reasoning output are recorded as informative subsets of the
    input/output totals and are therefore not added a second time by
    :attr:`total_tokens`.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise TypeError("Token counters must be integers")
        if min(values) < 0:
            raise ValueError("Token counters cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("Reasoning output tokens cannot exceed output tokens")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def dominates(self, previous: TokenUsage) -> bool:
        """Return whether every cumulative counter is monotonic."""

        return all(
            current >= prior
            for current, prior in zip(
                (
                    self.input_tokens,
                    self.cached_input_tokens,
                    self.output_tokens,
                    self.reasoning_output_tokens,
                ),
                (
                    previous.input_tokens,
                    previous.cached_input_tokens,
                    previous.output_tokens,
                    previous.reasoning_output_tokens,
                ),
                strict=True,
            )
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenUsage:
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            cached_input_tokens=int(data.get("cached_input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            reasoning_output_tokens=int(
                data.get("reasoning_output_tokens", data.get("reasoning_tokens", 0))
            ),
        )


@dataclass(frozen=True, slots=True)
class UsageSample(Serializable):
    """Append-only cumulative usage reading for one run and source."""

    run_id: str
    thread_id: str
    turn_id: str
    sequence: int
    cumulative_quota: float
    unit: QuotaUnit = QuotaUnit.ABSTRACT
    source: str = "driver"
    tokens: TokenUsage | None = None
    delta: float | None = None
    id: str = field(default_factory=new_id)
    observed_at: datetime = field(default_factory=utc_now)
    provider_epoch: str | None = None
    final: bool = False
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("A usage sample requires a run identifier")
        if not self.thread_id.strip() or not self.turn_id.strip():
            raise ValueError("A usage sample requires thread and turn identifiers")
        if self.sequence < 0:
            raise ValueError("Usage sequence cannot be negative")
        if not isfinite(self.cumulative_quota) or self.cumulative_quota < 0:
            raise ValueError("Cumulative quota must be finite and non-negative")
        if (
            self.unit is QuotaUnit.TOKENS
            and self.tokens is not None
            and self.cumulative_quota != self.tokens.total_tokens
        ):
            raise ValueError(
                "Token usage counters must equal the cumulative token quota"
            )
        if self.delta is not None and (
            not isfinite(self.delta)
            or self.delta < 0
            or (self.delta == 0 and not self.final)
        ):
            raise ValueError(
                "An applied usage delta must be positive, or zero for a final marker"
            )
        if not self.source.strip():
            raise ValueError("A usage sample requires a source")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageSample:
        tokens = data.get("tokens")
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            thread_id=str(data["thread_id"]),
            turn_id=str(data["turn_id"]),
            sequence=int(data["sequence"]),
            cumulative_quota=float(data["cumulative_quota"]),
            unit=QuotaUnit(data.get("unit", QuotaUnit.ABSTRACT)),
            source=str(data.get("source", "driver")),
            tokens=TokenUsage.from_dict(tokens) if isinstance(tokens, dict) else None,
            delta=(float(data["delta"]) if data.get("delta") is not None else None),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
            provider_epoch=(
                str(data["provider_epoch"]) if data.get("provider_epoch") else None
            ),
            final=bool(data.get("final", False)),
            metadata=dict(data.get("metadata", {})),
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
    base_ref: str = "HEAD"
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
    artifact_inputs: tuple[ArtifactInput, ...] = ()
    artifact_outputs: tuple[ArtifactSpec, ...] = ()
    operation: JobOperation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_ref, str):
            raise TypeError("A job base ref must be a string")
        stripped = self.base_ref.strip()
        if (
            not stripped
            or stripped != self.base_ref
            or stripped.startswith("-")
            or "\0" in self.base_ref
        ):
            raise ValueError("A job base ref must be non-empty and not option-like")
        if not all(
            isinstance(item, (ArtifactRef, ArtifactSelector))
            for item in self.artifact_inputs
        ):
            raise TypeError(
                "Job artifact_inputs must contain ArtifactRef or "
                "ArtifactSelector values"
            )
        if len(set(self.artifact_inputs)) != len(self.artifact_inputs):
            raise ValueError("Job artifact_inputs cannot contain duplicates")
        if any(not isinstance(item, ArtifactSpec) for item in self.artifact_outputs):
            raise TypeError("Job artifact_outputs must contain ArtifactSpec values")
        output_names = tuple(item.name for item in self.artifact_outputs)
        if len(set(output_names)) != len(output_names):
            raise ValueError("Job artifact_outputs must have unique names")
        if self.operation is not None and not isinstance(
            self.operation, (BuildImageOperation, DeployImageOperation)
        ):
            raise TypeError("Job operation must be a supported typed operation")

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
            base_ref=str(data.get("base_ref", "HEAD")),
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
            artifact_inputs=tuple(
                artifact_input_from_dict(item)
                for item in data.get("artifact_inputs", [])
            ),
            artifact_outputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in data.get("artifact_outputs", [])
            ),
            operation=(
                operation_from_dict(data["operation"])
                if data.get("operation") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat(Serializable):
    """Last authenticated status reported by a configured remote worker."""

    session_epoch: str
    drivers: frozenset[str]
    active_runs: int
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.session_epoch, str) or not self.session_epoch.strip():
            raise ValueError("A worker heartbeat requires a session epoch")
        if any(
            not isinstance(driver, str) or not driver.strip() for driver in self.drivers
        ):
            raise ValueError("Worker heartbeat drivers must be non-empty names")
        if (
            isinstance(self.active_runs, bool)
            or not isinstance(self.active_runs, int)
            or self.active_runs < 0
        ):
            raise ValueError("Worker heartbeat active_runs must be non-negative")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerHeartbeat:
        return cls(
            session_epoch=str(data["session_epoch"]),
            drivers=frozenset(str(item) for item in data.get("drivers", [])),
            active_runs=int(data["active_runs"]),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
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
    heartbeat: WorkerHeartbeat | None = None

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
            heartbeat=(
                WorkerHeartbeat.from_dict(data["heartbeat"])
                if data.get("heartbeat") is not None
                else None
            ),
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
    debt: float = 0
    unit: QuotaUnit = QuotaUnit.ABSTRACT
    reset_at: datetime | None = None
    reset_confidence: float = 0
    minimum_interactive_reserve: float = 0
    mode: QuotaMode = QuotaMode.NORMAL
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        amounts = (
            self.remaining,
            self.reserved,
            self.debt,
            self.minimum_interactive_reserve,
        )
        if any(not isfinite(amount) or amount < 0 for amount in amounts):
            raise ValueError("Quota values must be finite and non-negative")
        if not 0 <= self.reset_confidence <= 1:
            raise ValueError("Reset confidence must be between zero and one")

    @property
    def dispatchable(self) -> float:
        return max(0, self.remaining - self.reserved - self.debt)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaPool:
        return cls(
            id=str(data["id"]),
            provider=str(data["provider"]),
            remaining=float(data["remaining"]),
            reserved=float(data.get("reserved", 0)),
            debt=float(data.get("debt", 0)),
            unit=QuotaUnit(data.get("unit", QuotaUnit.ABSTRACT)),
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
class ProviderQuotaSnapshot(Serializable):
    """Raw ChatGPT quota-window observation for one provider limit bucket.

    App Server exposes percentages and reset windows, not convertible token or
    credit balances. Credits therefore remain opaque JSON and this record never
    fabricates an absolute ``remaining`` value.
    """

    pool_id: str
    bucket_id: str
    provider: str = "openai-codex-chatgpt"
    primary_used_percent: float | None = None
    primary_window_minutes: int | None = None
    primary_reset_at: datetime | None = None
    secondary_used_percent: float | None = None
    secondary_window_minutes: int | None = None
    secondary_reset_at: datetime | None = None
    reached: bool = False
    credits_exhausted: bool | None = None
    rate_limit_reached_type: str | None = None
    plan_type: str | None = None
    credits: JsonValue = None
    rate_limit_reset_credits: JsonValue = None
    id: str = field(default_factory=new_id)
    observed_at: datetime = field(default_factory=utc_now)
    confidence: float = 1
    source: str = "codex-app-server"
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pool_id.strip() or not self.bucket_id.strip():
            raise ValueError("A provider snapshot requires pool and bucket identifiers")
        for used_percent in (
            self.primary_used_percent,
            self.secondary_used_percent,
        ):
            if used_percent is not None and (
                not isfinite(used_percent) or not 0 <= used_percent <= 100
            ):
                raise ValueError("Provider used percentages must be between 0 and 100")
        for window in (
            self.primary_window_minutes,
            self.secondary_window_minutes,
        ):
            if window is not None and window <= 0:
                raise ValueError("Provider window minutes must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError(
                "Provider snapshot confidence must be between zero and one"
            )
        if not self.source.strip():
            raise ValueError("A provider snapshot requires a source")

    @property
    def reset_at(self) -> datetime | None:
        resets = tuple(
            reset
            for reset in (self.primary_reset_at, self.secondary_reset_at)
            if reset is not None
        )
        return min(resets) if resets else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderQuotaSnapshot:
        def optional_datetime(name: str) -> datetime | None:
            return datetime.fromisoformat(str(data[name])) if data.get(name) else None

        return cls(
            id=str(data["id"]),
            pool_id=str(data["pool_id"]),
            bucket_id=str(data["bucket_id"]),
            provider=str(data.get("provider", "openai-codex-chatgpt")),
            primary_used_percent=(
                float(data["primary_used_percent"])
                if data.get("primary_used_percent") is not None
                else None
            ),
            primary_window_minutes=(
                int(data["primary_window_minutes"])
                if data.get("primary_window_minutes") is not None
                else None
            ),
            primary_reset_at=optional_datetime("primary_reset_at"),
            secondary_used_percent=(
                float(data["secondary_used_percent"])
                if data.get("secondary_used_percent") is not None
                else None
            ),
            secondary_window_minutes=(
                int(data["secondary_window_minutes"])
                if data.get("secondary_window_minutes") is not None
                else None
            ),
            secondary_reset_at=optional_datetime("secondary_reset_at"),
            reached=bool(data.get("reached", False)),
            credits_exhausted=(
                bool(data["credits_exhausted"])
                if data.get("credits_exhausted") is not None
                else None
            ),
            rate_limit_reached_type=(
                str(data["rate_limit_reached_type"])
                if data.get("rate_limit_reached_type")
                else None
            ),
            plan_type=str(data["plan_type"]) if data.get("plan_type") else None,
            credits=data.get("credits"),
            rate_limit_reset_credits=data.get("rate_limit_reset_credits"),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
            confidence=float(data.get("confidence", 1)),
            source=str(data.get("source", "codex-app-server")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class QuotaReservation(Serializable):
    job_id: str
    pool_id: str
    amount: float
    id: str = field(default_factory=new_id)
    state: ReservationState = ReservationState.ACTIVE
    consumed: float = 0
    debt: float = 0
    unit: QuotaUnit = QuotaUnit.ABSTRACT
    created_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not isfinite(amount) or amount < 0
            for amount in (self.amount, self.consumed, self.debt)
        ):
            raise ValueError("Reservation amounts must be finite and non-negative")

    @property
    def outstanding(self) -> float:
        return max(0, self.amount - self.consumed)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaReservation:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            pool_id=str(data["pool_id"]),
            amount=float(data["amount"]),
            state=ReservationState(data.get("state", ReservationState.ACTIVE)),
            consumed=float(data.get("consumed", 0)),
            debt=float(data.get("debt", 0)),
            unit=QuotaUnit(data.get("unit", QuotaUnit.ABSTRACT)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            released_at=(
                datetime.fromisoformat(str(data["released_at"]))
                if data.get("released_at")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class UsageApplication:
    """Result of atomically applying one cumulative usage sample."""

    sample: UsageSample
    delta: float
    duplicate: bool
    reservation: QuotaReservation
    pool: QuotaPool
    job_consumed: float
    maximum: float | None = None
    maximum_exceeded: bool = False
    debt_incurred: float = 0


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
    artifact_inputs: tuple[ArtifactRef, ...] = ()
    artifact_outputs: tuple[ArtifactSpec, ...] = ()
    operation: JobOperation | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(item, ArtifactRef) for item in self.artifact_inputs):
            raise TypeError(
                "ExecutionContract artifact_inputs must be resolved ArtifactRef values"
            )
        if self.operation is not None:
            operation_input = (
                self.operation.source_input
                if isinstance(self.operation, BuildImageOperation)
                else self.operation.image_input
            )
            if isinstance(operation_input, ArtifactSelector):
                raise ValueError("ExecutionContract operation inputs must be resolved")

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
            artifact_inputs=tuple(
                ArtifactRef.from_dict(item) for item in data.get("artifact_inputs", [])
            ),
            artifact_outputs=tuple(
                ArtifactSpec.from_dict(item)
                for item in data.get("artifact_outputs", [])
            ),
            operation=(
                operation_from_dict(data["operation"])
                if data.get("operation") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class RunHandle(Serializable):
    id: str
    driver: str
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunObservation(Serializable):
    """SDK-neutral observation of a live or terminal harness run."""

    run_id: str
    thread_id: str
    turn_id: str
    cursor: str
    terminal: bool = False
    telemetry_valid: bool = True
    usage: TokenUsage | None = None
    cumulative_quota: float | None = None
    unit: QuotaUnit = QuotaUnit.ABSTRACT
    source: str = "driver"
    run_state: RunState | None = None
    result: RunResult | None = None
    provider_epoch: str | None = None
    observed_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("A run observation requires a run identifier")
        if not self.thread_id.strip() or not self.turn_id.strip():
            raise ValueError("A run observation requires thread and turn identifiers")
        if not self.cursor.strip():
            raise ValueError("A run observation requires a cursor")
        if not self.source.strip():
            raise ValueError("A run observation requires a source")
        if self.cumulative_quota is not None and (
            not isfinite(self.cumulative_quota) or self.cumulative_quota < 0
        ):
            raise ValueError(
                "Observed cumulative quota must be finite and non-negative"
            )
        if self.result is not None and not self.terminal:
            raise ValueError("A terminal result requires a terminal observation")

    @property
    def normalized_cumulative_quota(self) -> float | None:
        if self.cumulative_quota is not None:
            return self.cumulative_quota
        if self.unit is QuotaUnit.TOKENS and self.usage is not None:
            return float(self.usage.total_tokens)
        return None

    def to_usage_sample(self, sequence: int) -> UsageSample:
        if not self.telemetry_valid:
            raise ValueError("Invalid telemetry cannot become a usage sample")
        cumulative = self.normalized_cumulative_quota
        if cumulative is None:
            raise ValueError("The observation has no normalized cumulative usage")
        return UsageSample(
            run_id=self.run_id,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            sequence=sequence,
            cumulative_quota=cumulative,
            unit=self.unit,
            source=self.source,
            tokens=self.usage,
            observed_at=self.observed_at,
            provider_epoch=self.provider_epoch,
            final=self.terminal,
            metadata=self.metadata,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunObservation:
        usage = data.get("usage")
        run_state = data.get("run_state")
        result = data.get("result")
        return cls(
            run_id=str(data["run_id"]),
            thread_id=str(data["thread_id"]),
            turn_id=str(data["turn_id"]),
            cursor=str(data["cursor"]),
            terminal=bool(data.get("terminal", False)),
            telemetry_valid=bool(data.get("telemetry_valid", True)),
            usage=TokenUsage.from_dict(usage) if isinstance(usage, dict) else None,
            cumulative_quota=(
                float(data["cumulative_quota"])
                if data.get("cumulative_quota") is not None
                else None
            ),
            unit=QuotaUnit(data.get("unit", QuotaUnit.ABSTRACT)),
            source=str(data.get("source", "driver")),
            run_state=RunState(run_state) if run_state is not None else None,
            result=RunResult.from_dict(result) if isinstance(result, dict) else None,
            provider_epoch=(
                str(data["provider_epoch"]) if data.get("provider_epoch") else None
            ),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class ProducedArtifact(Serializable):
    """A run output tied to the stable output slot it satisfies."""

    spec_name: str
    ref: ArtifactRef
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_stable_name(self.spec_name, "Produced artifact spec name")
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError("Produced artifact ref must be an ArtifactRef")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProducedArtifact:
        return cls(
            spec_name=str(data["spec_name"]),
            ref=ArtifactRef.from_dict(data["ref"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class RunResult(Serializable):
    outcome: RunOutcome
    summary: str = ""
    commit: str | None = None
    consumed_quota: float = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    usage: TokenUsage | None = None
    produced_artifacts: tuple[ProducedArtifact, ...] = ()

    def __post_init__(self) -> None:
        if self.consumed_quota < 0 or not isfinite(self.consumed_quota):
            raise ValueError("Consumed quota must be finite and non-negative")
        if self.usage is None:
            raw_usage = self.metadata.get("usage")
            token_keys = {
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "reasoning_output_tokens",
            }
            if isinstance(raw_usage, dict) and token_keys.intersection(raw_usage):
                try:
                    parsed = TokenUsage.from_dict(raw_usage)
                except (TypeError, ValueError):
                    pass
                else:
                    object.__setattr__(self, "usage", parsed)
        if any(
            not isinstance(artifact, ProducedArtifact)
            for artifact in self.produced_artifacts
        ):
            raise TypeError(
                "Run produced_artifacts must contain ProducedArtifact values"
            )
        spec_names = tuple(artifact.spec_name for artifact in self.produced_artifacts)
        if len(set(spec_names)) != len(spec_names):
            raise ValueError("Run produced_artifacts must have unique spec names")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        usage = data.get("usage")
        return cls(
            outcome=RunOutcome(data["outcome"]),
            summary=str(data.get("summary", "")),
            commit=str(data["commit"]) if data.get("commit") else None,
            consumed_quota=float(data.get("consumed_quota", 0)),
            metadata=dict(data.get("metadata", {})),
            usage=TokenUsage.from_dict(usage) if isinstance(usage, dict) else None,
            produced_artifacts=tuple(
                ProducedArtifact.from_dict(item)
                for item in data.get("produced_artifacts", [])
            ),
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
class DriverSession(Serializable):
    """Durable driver identity and observation watermark for one run."""

    run_id: str
    driver: str
    id: str = field(default_factory=new_id)
    external_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    observation_cursor: str | None = None
    last_observation: RunObservation | None = None
    active: bool = True
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.driver.strip():
            raise ValueError("A driver session requires run and driver identifiers")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriverSession:
        observation = data.get("last_observation")
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            driver=str(data["driver"]),
            external_id=(str(data["external_id"]) if data.get("external_id") else None),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
            turn_id=str(data["turn_id"]) if data.get("turn_id") else None,
            observation_cursor=(
                str(data["observation_cursor"])
                if data.get("observation_cursor")
                else None
            ),
            last_observation=(
                RunObservation.from_dict(observation)
                if isinstance(observation, dict)
                else None
            ),
            active=bool(data.get("active", True)),
            metadata=dict(data.get("metadata", {})),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class RunCommand(Serializable):
    """Durable scheduler-to-driver command."""

    run_id: str
    action: str
    id: str = field(default_factory=new_id)
    payload: dict[str, JsonValue] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.action.strip():
            raise ValueError("A run command requires run and action identifiers")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunCommand:
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            action=str(data["action"]),
            payload=dict(data.get("payload", {})),
            created_at=datetime.fromisoformat(str(data["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class RunCommandAck(Serializable):
    """Append-only acknowledgement for a durable run command."""

    command_id: str
    run_id: str
    accepted: bool = True
    detail: str = ""
    observation_cursor: str | None = None
    id: str = field(default_factory=new_id)
    acknowledged_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunCommandAck:
        return cls(
            id=str(data["id"]),
            command_id=str(data["command_id"]),
            run_id=str(data["run_id"]),
            accepted=bool(data.get("accepted", True)),
            detail=str(data.get("detail", "")),
            observation_cursor=(
                str(data["observation_cursor"])
                if data.get("observation_cursor")
                else None
            ),
            acknowledged_at=datetime.fromisoformat(str(data["acknowledged_at"])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class AgentRequestRecord(Serializable):
    """Append-only durable refinement or blocker message from a worker."""

    request_id: str
    sequence: int
    run_id: str
    kind: AgentRequestKind
    message: str
    created_at: datetime
    retryable: bool | None = None
    # Kept at the end with a default so callers of the original in-process
    # record constructor retain positional compatibility. Durable stores fill
    # this ownership field before persisting the record.
    job_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.run_id.strip():
            raise ValueError("An agent request requires request and run identifiers")
        if self.job_id and not self.job_id.strip():
            raise ValueError("An agent request job identifier cannot be blank")
        if self.sequence < 0:
            raise ValueError("Agent request sequence cannot be negative")
        if not isinstance(self.kind, AgentRequestKind):
            raise TypeError("Agent request kind must be an AgentRequestKind")
        if not self.message.strip():
            raise ValueError("Agent request message cannot be empty")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise TypeError("Agent request retryable must be a bool or None")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRequestRecord:
        return cls(
            request_id=str(data["request_id"]),
            sequence=int(data["sequence"]),
            run_id=str(data["run_id"]),
            kind=AgentRequestKind(data["kind"]),
            message=str(data["message"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            retryable=(
                bool(data["retryable"]) if data.get("retryable") is not None else None
            ),
            job_id=str(data.get("job_id", "")),
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
    id: str = field(default_factory=new_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuotaResetEvent:
        return cls(
            id=str(data["id"]) if data.get("id") else new_id(),
            pool_id=str(data["pool_id"]),
            mode=QuotaMode(data["mode"]),
            expected_reset_at=(
                datetime.fromisoformat(str(data["expected_reset_at"]))
                if data.get("expected_reset_at")
                else None
            ),
            confidence=float(data.get("confidence", 0)),
            new_remaining=(
                float(data["new_remaining"])
                if data.get("new_remaining") is not None
                else None
            ),
            source=str(data.get("source", "external-oracle")),
        )
