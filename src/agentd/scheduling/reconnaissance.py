"""Bounded reconnaissance compilation for hors-categorie jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from agentd.domain.enums import (
    CheckpointPolicy,
    JobState,
    PreemptionPolicy,
    QoSClass,
)
from agentd.domain.models import (
    BurnPolicy,
    EffortEstimate,
    Job,
    QuotaBudget,
)

RECONNAISSANCE_ACCEPTANCE_CRITERIA = (
    "Identify material unknowns and risks",
    "Produce a dependency-aware bounded execution plan",
    "Identify safe checkpoint boundaries",
    "Estimate effort and accepted-artifact quota for subsequent work",
)


@dataclass(frozen=True, slots=True)
class ReconnaissanceLimits:
    """Hard effort and quota limits for a reconnaissance slice."""

    p50: float = 5
    p90: float = 10
    p99: float = 15
    quota: float = 10
    qos: QoSClass = QoSClass.NORMAL

    def __post_init__(self) -> None:
        # Reuse domain validation and also reject an unbounded dispatch class.
        EffortEstimate(self.p50, self.p90, self.p99)
        if self.quota < 0:
            raise ValueError("Reconnaissance quota cannot be negative")
        if self.qos is QoSClass.HORS_CATEGORIE:
            raise ValueError("A reconnaissance slice must have a bounded QoS class")


@dataclass(frozen=True, slots=True)
class ReconnaissanceOutcome:
    """Structured result needed before a parent job can be promoted."""

    execution_steps: tuple[str, ...]
    safe_checkpoint_boundaries: tuple[str, ...]
    effort: EffortEstimate
    quota_budget: QuotaBudget
    dependencies: tuple[str, ...] = ()
    refined_objective: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    sufficiently_bounded: bool = True


_DEFAULT_LIMITS = ReconnaissanceLimits()


def compile_reconnaissance(
    parent: Job,
    *,
    reconnaissance_id: str,
    at: datetime,
    limits: ReconnaissanceLimits = _DEFAULT_LIMITS,
) -> Job:
    """Compile an unbounded parent into a deterministic bounded planning job."""

    if parent.qos is not QoSClass.HORS_CATEGORIE:
        raise ValueError("Only hors-categorie jobs require reconnaissance")
    if parent.reconnaissance_for is not None:
        raise ValueError("Cannot recursively compile a reconnaissance job")
    if not reconnaissance_id.strip():
        raise ValueError("Reconnaissance ID cannot be empty")

    objective = (
        f"Reconnaissance only for job {parent.id}: inspect the objective, identify "
        "unknowns, decompose the work, identify dependencies and safe checkpoint "
        "boundaries, and estimate bounded follow-up effort. Do not implement the "
        f"full objective yet.\n\nParent objective: {parent.objective}"
    )
    effort = EffortEstimate(limits.p50, limits.p90, limits.p99, parent.effort.unit)
    quota_budget = QuotaBudget(
        implementation=limits.quota,
        maximum=limits.quota,
        pool_id=parent.quota_budget.pool_id,
        unit=parent.quota_budget.unit,
    )

    return replace(
        parent,
        id=reconnaissance_id,
        objective=objective,
        effort=effort,
        quota_budget=quota_budget,
        qos=limits.qos,
        preemption_policy=PreemptionPolicy.CHECKPOINT,
        checkpoint_policy=CheckpointPolicy.ON_REQUEST,
        acceptance_criteria=RECONNAISSANCE_ACCEPTANCE_CRITERIA,
        burn=BurnPolicy(eligible=False, checkpointable=True),
        state=JobState.BACKLOG,
        gang_id=None,
        reconnaissance_for=parent.id,
        selected_harness=None,
        selected_model_class=None,
        created_at=at,
        updated_at=at,
    )


def is_promotable(outcome: ReconnaissanceOutcome) -> bool:
    """Require a finite execution tail, budget cap, plan, and checkpoint boundary."""

    return (
        outcome.sufficiently_bounded
        and bool(outcome.execution_steps)
        and bool(outcome.safe_checkpoint_boundaries)
        and outcome.effort.p99 is not None
        and outcome.quota_budget.maximum is not None
    )


def promote_hors_categorie(
    parent: Job,
    outcome: ReconnaissanceOutcome,
    *,
    at: datetime,
    qos: QoSClass = QoSClass.NORMAL,
) -> Job:
    """Return a bounded parent specification without changing lifecycle state.

    The coordinator remains responsible for the audited ``PLANNING -> READY``
    transition after persisting the reconnaissance result.
    """

    if parent.qos is not QoSClass.HORS_CATEGORIE:
        raise ValueError("Only hors-categorie jobs can be promoted")
    if qos is QoSClass.HORS_CATEGORIE:
        raise ValueError("Promotion requires a bounded QoS class")
    if not is_promotable(outcome):
        raise ValueError("Reconnaissance outcome is not sufficiently bounded")

    dependencies = tuple(dict.fromkeys((*parent.dependencies, *outcome.dependencies)))
    acceptance_criteria = outcome.acceptance_criteria or parent.acceptance_criteria
    return replace(
        parent,
        objective=outcome.refined_objective or parent.objective,
        dependencies=dependencies,
        effort=outcome.effort,
        quota_budget=outcome.quota_budget,
        qos=qos,
        acceptance_criteria=acceptance_criteria,
        updated_at=at,
    )
