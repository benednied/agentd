"""Persistence interface for control-plane execution state."""

from collections.abc import Iterable
from typing import Protocol

from agentd.domain.enums import JobState
from agentd.domain.models import (
    AgentRequestRecord,
    ArtifactRecord,
    Checkpoint,
    DriverSession,
    Job,
    ProviderQuotaSnapshot,
    QuotaPool,
    QuotaReservation,
    QuotaResetEvent,
    ResourceAllocation,
    RunCommand,
    RunCommandAck,
    RunObservation,
    RunRecord,
    StateTransition,
    UsageApplication,
    UsageSample,
    WorkerNode,
    WorkspaceLease,
)


class EntityNotFoundError(LookupError):
    """Raised when durable state lacks a requested domain entity."""

    pass


class ConcurrentStateError(RuntimeError):
    """Raised when optimistic state assumptions no longer hold."""

    pass


class StateStore(Protocol):
    """Durable state operations required by control-plane services."""

    def initialize(self) -> None: ...

    def close(self) -> None: ...

    def create_job(self, job: Job, transition: StateTransition) -> None: ...

    def save_job(
        self,
        job: Job,
        transition: StateTransition | None = None,
        *,
        expected: Job,
    ) -> None: ...

    def save_job_and_run(
        self,
        job: Job,
        transition: StateTransition,
        run: RunRecord,
        *,
        expected_job: Job,
        expected_run: RunRecord | None,
    ) -> None: ...

    def save_job_and_run_with_artifacts(
        self,
        job: Job,
        transition: StateTransition,
        run: RunRecord,
        artifacts: Iterable[ArtifactRecord],
        *,
        expected_job: Job,
        expected_run: RunRecord | None,
    ) -> None: ...

    def publish_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord: ...

    def register_external_artifact(
        self, artifact: ArtifactRecord
    ) -> ArtifactRecord: ...

    def publish_artifacts(
        self, artifacts: Iterable[ArtifactRecord]
    ) -> tuple[ArtifactRecord, ...]: ...

    def get_artifact(self, artifact_id: str) -> ArtifactRecord: ...

    def list_artifacts(
        self,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
    ) -> list[ArtifactRecord]: ...

    def append_agent_request(
        self, request: AgentRequestRecord
    ) -> AgentRequestRecord: ...

    def list_agent_requests(
        self, run_id: str, *, limit: int | None = None
    ) -> list[AgentRequestRecord]: ...

    def get_job(self, job_id: str) -> Job: ...

    def list_jobs(self, states: frozenset[JobState] | None = None) -> list[Job]: ...

    def list_transitions(self, job_id: str) -> list[StateTransition]: ...

    def save_node(self, node: WorkerNode) -> None: ...

    def register_node(self, node: WorkerNode) -> WorkerNode: ...

    def get_node(self, node_id: str) -> WorkerNode: ...

    def list_nodes(self) -> list[WorkerNode]: ...

    def save_allocation(self, allocation: ResourceAllocation) -> None: ...

    def allocate_resources(
        self,
        expected_node: WorkerNode,
        updated_node: WorkerNode,
        allocation: ResourceAllocation,
    ) -> None: ...

    def release_resources(
        self,
        expected_node: WorkerNode,
        updated_node: WorkerNode,
        expected_allocation: ResourceAllocation,
        released_allocation: ResourceAllocation,
    ) -> None: ...

    def get_allocation(self, allocation_id: str) -> ResourceAllocation: ...

    def find_active_allocation(self, job_id: str) -> ResourceAllocation | None: ...

    def list_allocations(
        self, job_id: str | None = None
    ) -> list[ResourceAllocation]: ...

    def save_quota_pool(self, pool: QuotaPool) -> None: ...

    def register_quota_pool(self, pool: QuotaPool) -> QuotaPool: ...

    def update_quota_pool(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
    ) -> None: ...

    def apply_reset_event(self, event: QuotaResetEvent) -> QuotaPool: ...

    def get_reset_event(self, event_id: str) -> QuotaResetEvent: ...

    def list_reset_events(
        self, pool_id: str | None = None
    ) -> list[QuotaResetEvent]: ...

    def get_quota_pool(self, pool_id: str) -> QuotaPool: ...

    def list_quota_pools(self) -> list[QuotaPool]: ...

    def save_reservation(self, reservation: QuotaReservation) -> None: ...

    def reserve_quota(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
        reservation: QuotaReservation,
    ) -> None: ...

    def release_quota(
        self,
        expected_pool: QuotaPool,
        updated_pool: QuotaPool,
        expected_reservation: QuotaReservation,
        released_reservation: QuotaReservation,
    ) -> None: ...

    def get_reservation(self, reservation_id: str) -> QuotaReservation: ...

    def find_active_reservation(self, job_id: str) -> QuotaReservation | None: ...

    def list_reservations(
        self, job_id: str | None = None
    ) -> list[QuotaReservation]: ...

    def apply_usage_sample(
        self,
        sample: UsageSample,
        *,
        maximum: float | None = None,
    ) -> UsageApplication: ...

    def list_usage_samples(self, run_id: str) -> list[UsageSample]: ...

    def top_up_quota(
        self,
        reservation_id: str,
        amount: float,
        *,
        minimum_dispatchable: float = 0,
    ) -> QuotaReservation: ...

    def begin_metering(
        self,
        job: Job,
        transition: StateTransition,
        reservation_id: str,
        *,
        expected_job: Job,
        run: RunRecord | None = None,
        expected_run: RunRecord | None = None,
    ) -> QuotaReservation: ...

    def settle_quota_usage(
        self,
        reservation_id: str,
        *,
        final_sample: UsageSample | None = None,
        cancelled: bool = False,
        maximum: float | None = None,
    ) -> QuotaReservation: ...

    def save_workspace(
        self,
        workspace: WorkspaceLease,
        *,
        expected: WorkspaceLease | None,
    ) -> None: ...

    def get_workspace(self, workspace_id: str) -> WorkspaceLease: ...

    def find_workspace(self, job_id: str) -> WorkspaceLease | None: ...

    def list_workspaces(self, job_id: str | None = None) -> list[WorkspaceLease]: ...

    def save_run(self, run: RunRecord, *, expected: RunRecord | None) -> None: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def find_active_run(self, job_id: str) -> RunRecord | None: ...

    def latest_run(self, job_id: str) -> RunRecord | None: ...

    def list_runs(self, job_id: str | None = None) -> list[RunRecord]: ...

    def save_driver_session(
        self,
        session: DriverSession,
        *,
        expected: DriverSession | None,
    ) -> None: ...

    def get_driver_session(self, run_id: str) -> DriverSession: ...

    def list_driver_sessions(
        self, active: bool | None = None
    ) -> list[DriverSession]: ...

    def update_observation_cursor(
        self,
        run_id: str,
        expected_cursor: str | None,
        cursor: str,
        observation: RunObservation | None = None,
    ) -> DriverSession: ...

    def append_provider_quota_snapshot(
        self, snapshot: ProviderQuotaSnapshot
    ) -> None: ...

    def latest_provider_quota_snapshot(
        self,
        pool_id: str,
        bucket_id: str | None = None,
    ) -> ProviderQuotaSnapshot | None: ...

    def list_provider_quota_snapshots(
        self,
        pool_id: str,
        bucket_id: str | None = None,
    ) -> list[ProviderQuotaSnapshot]: ...

    def enqueue_run_command(self, command: RunCommand) -> None: ...

    def list_run_commands(self, run_id: str) -> list[RunCommand]: ...

    def list_pending_run_commands(
        self, run_id: str | None = None
    ) -> list[RunCommand]: ...

    def acknowledge_run_command(self, acknowledgement: RunCommandAck) -> None: ...

    def get_run_command_ack(self, command_id: str) -> RunCommandAck | None: ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...

    def latest_checkpoint(self, job_id: str) -> Checkpoint | None: ...

    def list_checkpoints(self, job_id: str) -> list[Checkpoint]: ...
