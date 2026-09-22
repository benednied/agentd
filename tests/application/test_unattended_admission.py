import asyncio
from dataclasses import replace

from agentd.domain.enums import JobState, QoSClass
from agentd.domain.models import ProviderQuotaSnapshot
from agentd.runtime.quota import QuotaManager


def test_scavenger_waits_before_workspace_and_dispatches_after_provider_observation(
    make_application_rig, make_application_job, application_node, application_quota_pool
):
    rig = make_application_rig()
    job = make_application_job(qos=QoSClass.SCAVENGER)
    job = replace(job, quota_budget=replace(job.quota_budget, maximum=20))

    async def scenario():
        rig.plane.register_node(application_node)
        rig.plane.register_quota_pool(application_quota_pool)
        rig.plane.submit(job)
        for _ in range(2):
            assert await rig.plane.dispatch_next() is None
        assert rig.store.get_job(job.id).state is JobState.READY
        assert rig.store.find_active_reservation(job.id) is None
        assert rig.store.find_workspace(job.id) is None
        assert QuotaManager(rig.store).wait_reason(job) == "quota_unknown"
        # The fake harness and disabled optional Codex policy cannot bypass this gate.
        rig.store.append_provider_quota_snapshot(
            ProviderQuotaSnapshot(
                pool_id="default", bucket_id="account", primary_used_percent=20
            )
        )
        run = await rig.plane.dispatch_next()
        assert run is not None
        assert run.job_id == job.id
        assert rig.store.get_quota_pool("default").reserved == 10
        assert await rig.plane.dispatch_next() is None

    asyncio.run(scenario())
