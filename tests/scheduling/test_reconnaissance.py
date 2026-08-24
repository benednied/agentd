from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import JobState, QoSClass, QuotaUnit
from agentd.domain.models import EffortEstimate, QuotaBudget
from agentd.scheduling.reconnaissance import (
    RECONNAISSANCE_ACCEPTANCE_CRITERIA,
    ReconnaissanceLimits,
    ReconnaissanceOutcome,
    compile_reconnaissance,
    is_promotable,
    promote_hors_categorie,
)


def test_compiles_hors_categorie_parent_into_bounded_reconnaissance(job_factory):
    now = datetime(2026, 3, 1, tzinfo=UTC)
    parent = job_factory(
        id="parent",
        qos=QoSClass.HORS_CATEGORIE,
        dependencies=("foundation",),
        allowed_harnesses=("codex",),
        preferred_harnesses=("codex",),
        quota_budget=QuotaBudget(
            implementation=100,
            maximum=100,
            pool_id="subscription",
            unit=QuotaUnit.TOKENS,
        ),
        effort=EffortEstimate(10, 100),
    )

    reconnaissance = compile_reconnaissance(
        parent,
        reconnaissance_id="recon",
        at=now,
        limits=ReconnaissanceLimits(p50=2, p90=4, p99=6, quota=7),
    )

    assert reconnaissance.id == "recon"
    assert reconnaissance.reconnaissance_for == "parent"
    assert reconnaissance.state is JobState.BACKLOG
    assert reconnaissance.qos is QoSClass.NORMAL
    assert reconnaissance.dependencies == ("foundation",)
    assert reconnaissance.effort == EffortEstimate(2, 4, 6)
    assert reconnaissance.quota_budget.expected_path == 7
    assert reconnaissance.quota_budget.maximum == 7
    assert reconnaissance.quota_budget.pool_id == "subscription"
    assert reconnaissance.quota_budget.unit is QuotaUnit.TOKENS
    assert reconnaissance.acceptance_criteria == RECONNAISSANCE_ACCEPTANCE_CRITERIA
    assert reconnaissance.created_at == now
    assert reconnaissance.updated_at == now
    assert "Do not implement the full objective yet" in reconnaissance.objective


def test_reconnaissance_compilation_rejects_normal_or_recursive_jobs(job_factory):
    at = datetime(2026, 3, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="Only hors-categorie"):
        compile_reconnaissance(job_factory(), reconnaissance_id="recon", at=at)
    with pytest.raises(ValueError, match="recursively"):
        compile_reconnaissance(
            job_factory(
                qos=QoSClass.HORS_CATEGORIE, reconnaissance_for="another-parent"
            ),
            reconnaissance_id="recon",
            at=at,
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        compile_reconnaissance(
            job_factory(qos=QoSClass.HORS_CATEGORIE),
            reconnaissance_id=" ",
            at=at,
        )


def test_promotes_only_structurally_bounded_outcomes_without_changing_state(
    job_factory,
):
    now = datetime(2026, 3, 2, tzinfo=UTC)
    parent = job_factory(
        id="parent",
        qos=QoSClass.HORS_CATEGORIE,
        state=JobState.PLANNING,
        dependencies=("existing",),
        acceptance_criteria=("old criterion",),
    )
    outcome = ReconnaissanceOutcome(
        execution_steps=("schema", "implementation", "review"),
        safe_checkpoint_boundaries=("after schema",),
        dependencies=("new", "existing"),
        effort=EffortEstimate(10, 20, 30),
        quota_budget=QuotaBudget(implementation=20, review=5, validation=5, maximum=40),
        refined_objective="Implement the bounded plan",
        acceptance_criteria=("new criterion",),
    )

    promoted = promote_hors_categorie(parent, outcome, at=now)

    assert is_promotable(outcome)
    assert promoted.id == parent.id
    assert promoted.qos is QoSClass.NORMAL
    assert promoted.state is JobState.PLANNING
    assert promoted.dependencies == ("existing", "new")
    assert promoted.objective == "Implement the bounded plan"
    assert promoted.acceptance_criteria == ("new criterion",)
    assert promoted.effort == outcome.effort
    assert promoted.quota_budget == outcome.quota_budget
    assert promoted.created_at == parent.created_at
    assert promoted.updated_at == now


@pytest.mark.parametrize(
    "outcome",
    [
        ReconnaissanceOutcome(
            execution_steps=(),
            safe_checkpoint_boundaries=("boundary",),
            effort=EffortEstimate(1, 2, 3),
            quota_budget=QuotaBudget(1, maximum=1),
        ),
        ReconnaissanceOutcome(
            execution_steps=("step",),
            safe_checkpoint_boundaries=(),
            effort=EffortEstimate(1, 2, 3),
            quota_budget=QuotaBudget(1, maximum=1),
        ),
        ReconnaissanceOutcome(
            execution_steps=("step",),
            safe_checkpoint_boundaries=("boundary",),
            effort=EffortEstimate(1, 2),
            quota_budget=QuotaBudget(1, maximum=1),
        ),
        ReconnaissanceOutcome(
            execution_steps=("step",),
            safe_checkpoint_boundaries=("boundary",),
            effort=EffortEstimate(1, 2, 3),
            quota_budget=QuotaBudget(1),
        ),
    ],
)
def test_rejects_unbounded_promotion(job_factory, outcome):
    parent = job_factory(qos=QoSClass.HORS_CATEGORIE)

    assert not is_promotable(outcome)
    with pytest.raises(ValueError, match="not sufficiently bounded"):
        promote_hors_categorie(
            parent,
            outcome,
            at=parent.updated_at + timedelta(seconds=1),
        )


def test_reconnaissance_limits_validate_bounded_policy():
    with pytest.raises(ValueError, match="bounded QoS"):
        ReconnaissanceLimits(qos=QoSClass.HORS_CATEGORIE)
    with pytest.raises(ValueError, match="quota"):
        ReconnaissanceLimits(quota=-1)
