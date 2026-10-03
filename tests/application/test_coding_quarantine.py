"""Quarantine frees capacity without settling or resurrecting unknown usage."""

import asyncio
from dataclasses import replace

import pytest
from test_coding_recovery import coding_rig

from agentd.coordinator import LifecycleError
from agentd.domain.enums import (
    AllocationState,
    JobState,
    ReservationState,
    RunState,
    WorkspaceState,
)
from agentd.domain.models import StateTransition, UsageSample, utc_now
from agentd.domain.transitions import transition_job
from agentd.runtime.resources import ResourceManager
from agentd.state.base import ConcurrentStateError
from agentd.workers.remote_protocol import payload_hash


def test_quarantine_preserves_accounting_and_rejects_late_mutations(
    tmp_path, monkeypatch
):
    async def scenario():
        async with coding_rig(tmp_path) as rig:
            rig.quota()
            run = await rig.plane.dispatch_next()
            await rig.coordinator.reconcile_managed_runs()
            run = rig.store.get_run(run.id)
            original_pool = rig.store.get_quota_pool("account")
            original_reservation = rig.store.get_reservation(run.reservation_id)
            original_usage = rig.store.list_usage_samples(run.id)
            original_session = rig.store.get_driver_session(run.id)
            proof = {
                "proof_version": 1,
                "job_id": rig.job.id,
                "node_id": run.node_id,
                "session_epoch": "epoch",
                "run_id": run.id,
                "start_hash": payload_hash(
                    {
                        "driver": run.driver,
                        "contract": run.contract.to_dict(),
                        "managed": True,
                    }
                ),
                "actor": "owner",
                "event_id": "comment:123",
                "container_id": "b" * 64,
                "stopped_at": utc_now().isoformat(),
                "running": False,
                "pid": 0,
            }
            calls = []

            async def quarantined(run_id, **kwargs):
                calls.append((run_id, kwargs))
                return {
                    **{
                        key: value
                        for key, value in proof.items()
                        if key not in {"running", "pid"}
                    },
                    "physical_retired": True,
                    "metering_unknown": True,
                    "stop_proof_sha256": payload_hash(proof),
                    "fenced_at": utc_now().isoformat(),
                }

            monkeypatch.setattr(rig.backend, "quarantine_run", quarantined)
            pending = await rig.coordinator.quarantine_run(
                run.id, actor="owner", event_id="comment:123", stop_proof=proof
            )
            retired = rig.store.get_run(run.id)
            assert pending.state is JobState.METERING_PENDING
            assert retired == replace(run, state=RunState.QUARANTINED)
            assert retired.result is run.result
            assert rig.store.get_quota_pool("account") == original_pool
            assert rig.store.get_reservation(run.reservation_id) == replace(
                original_reservation, state=ReservationState.METERING_PENDING
            )
            assert rig.store.list_usage_samples(run.id) == original_usage
            assert (
                rig.store.get_allocation(run.allocation_id).state
                is AllocationState.RELEASED
            )
            assert (
                rig.store.get_workspace(run.workspace_id).state
                is WorkspaceState.RETAINED
            )
            assert not rig.store.get_driver_session(run.id).active
            audit = rig.store.get_run_quarantine(run.id)
            assert (
                await rig.coordinator.quarantine_run(
                    run.id, actor="owner", event_id="comment:123", stop_proof=proof
                )
                == pending
            )
            assert len(calls) == 1
            with pytest.raises(LifecycleError):
                await rig.coordinator.quarantine_run(
                    run.id, actor="owner", event_id="comment:124", stop_proof=proof
                )
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.save_run(
                    replace(retired, state=RunState.RUNNING), expected=retired
                )
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.apply_usage_sample(
                    UsageSample(run.id, "late", "late", 1, 99, final=True)
                )
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.settle_quota_usage(run.reservation_id)
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.save_reservation(original_reservation)
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.update_observation_cursor(
                    run.id, original_session.observation_cursor, "late"
                )
            session = rig.store.get_driver_session(run.id)
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.save_driver_session(
                    replace(session, active=True), expected=session
                )
            cancelled, transition = transition_job(
                pending, JobState.CANCELLED, "late closure"
            )
            with pytest.raises(ConcurrentStateError, match="Quarantined"):
                rig.store.save_job(cancelled, transition, expected=pending)
            assert await rig.coordinator.cancel(rig.job.id) == pending
            with pytest.raises(LifecycleError):
                await rig.coordinator.complete(rig.job.id)
            with pytest.raises(LifecycleError):
                await rig.coordinator.queue_coding_repair(
                    rig.job.id, diagnostics=("retry",), actor="owner", event_id="124"
                )
            await rig.coordinator.reconcile_managed_runs()
            await rig.coordinator.recover_managed_runs()
            assert rig.store.get_run(run.id) == retired
            assert rig.store.get_run_quarantine(run.id) == audit
            assert rig.store.get_quota_pool("account") == original_pool
            assert rig.store.list_usage_samples(run.id) == original_usage
            assert (
                rig.coordinator._cleanup_execution_capacity(rig.job.id, retired) == []
            )
            # An independent issue gets the released physical capacity. Its
            # separate reservation does not consume the unknown attempt's hold.
            fresh = replace(
                rig.job,
                id="independent",
                operation=replace(
                    rig.job.operation,
                    work_order=replace(
                        rig.job.operation.work_order, job_id="independent"
                    ),
                ),
            )
            rig.store.create_job(
                fresh,
                StateTransition(fresh.id, None, fresh.state, "independent issue"),
            )
            allocation = ResourceManager(rig.store).allocate(
                fresh, rig.store.get_node(run.node_id)
            )
            assert allocation.state is AllocationState.ACTIVE
            assert rig.store.get_run_quarantine(run.id) == audit
            assert rig.store.get_quota_pool("account") == original_pool

    asyncio.run(scenario())
