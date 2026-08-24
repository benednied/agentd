import pytest

from agentd.domain.enums import (
    CheckpointPolicy,
    JobState,
    QoSClass,
    QuotaMode,
    TailAction,
)
from agentd.domain.models import BurnPolicy, EffortEstimate
from agentd.scheduling.burn import (
    declares_burn_eligibility,
    is_burn_candidate,
    order_jobs_for_quota_mode,
)
from agentd.scheduling.tail import TailThresholds, evaluate_tail


@pytest.mark.parametrize(
    ("consumed", "action"),
    [
        (10, TailAction.CONTINUE),
        (10.01, TailAction.REESTIMATE),
        (12.5, TailAction.REESTIMATE),
        (12.51, TailAction.CHECKPOINT_REPLAN),
        (20, TailAction.CHECKPOINT_REPLAN),
        (20.01, TailAction.CONVERT_TO_HORS_CATEGORIE),
    ],
)
def test_tail_governor_boundaries_with_finite_p99(consumed, action):
    estimate = EffortEstimate(p50=5, p90=10, p99=20)

    assert evaluate_tail(estimate, consumed).action is action


def test_tail_governor_uses_runaway_ratio_for_unbounded_tail():
    estimate = EffortEstimate(p50=5, p90=10)

    assert evaluate_tail(estimate, 15).action is TailAction.CHECKPOINT_REPLAN
    assert evaluate_tail(estimate, 20).action is TailAction.CHECKPOINT_REPLAN
    assert evaluate_tail(estimate, 20.1).action is TailAction.CONVERT_TO_HORS_CATEGORIE


def test_tail_governor_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="Consumed effort"):
        evaluate_tail(EffortEstimate(1, 2), -0.1)
    with pytest.raises(ValueError, match="Significant-overrun"):
        TailThresholds(significant_overrun_ratio=1)
    with pytest.raises(ValueError, match="Runaway ratio"):
        TailThresholds(significant_overrun_ratio=1.5, runaway_ratio=1.5)


def test_pre_reset_burn_promotes_only_ready_checkpointable_declarations(job_factory):
    urgent = job_factory(id="urgent", qos=QoSClass.INTERACTIVE, state=JobState.READY)
    burn = job_factory(
        id="burn",
        qos=QoSClass.SCAVENGER,
        state=JobState.READY,
        burn=BurnPolicy(eligible=True, checkpointable=True),
    )
    normal = job_factory(id="normal", qos=QoSClass.COMMITTED, state=JobState.READY)

    assert is_burn_candidate(burn, QuotaMode.PRE_RESET_BURN)
    assert [
        job.id
        for job in order_jobs_for_quota_mode(
            [normal, burn, urgent], QuotaMode.PRE_RESET_BURN
        )
    ] == ["urgent", "burn", "normal"]
    assert [
        job.id
        for job in order_jobs_for_quota_mode([burn, normal, urgent], QuotaMode.NORMAL)
    ] == ["urgent", "normal", "burn"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": JobState.BACKLOG},
        {"qos": QoSClass.HORS_CATEGORIE},
        {"checkpoint_policy": CheckpointPolicy.NONE},
        {"burn": BurnPolicy(eligible=False, checkpointable=True)},
        {"burn": BurnPolicy(eligible=True, checkpointable=False)},
    ],
)
def test_non_checkpointable_or_unbounded_work_is_not_a_burn_candidate(
    job_factory, overrides
):
    values = {
        "state": JobState.READY,
        "burn": BurnPolicy(eligible=True, checkpointable=True),
    }
    values.update(overrides)
    job = job_factory(**values)

    assert not is_burn_candidate(job, QuotaMode.PRE_RESET_BURN)
    if job.state is JobState.READY:
        assert declares_burn_eligibility(job) is (
            job.qos is not QoSClass.HORS_CATEGORIE
            and job.checkpoint_policy is not CheckpointPolicy.NONE
            and job.burn.eligible
            and job.burn.checkpointable
        )
