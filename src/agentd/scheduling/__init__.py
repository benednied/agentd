"""Pure scheduling policies used by the effectful control-plane coordinator."""

from agentd.scheduling.burn import (
    burn_order_key,
    declares_burn_eligibility,
    is_burn_candidate,
    order_jobs_for_quota_mode,
)
from agentd.scheduling.placement import (
    Placement,
    compatible_placements,
    select_placement,
)
from agentd.scheduling.priority import order_jobs, priority_key
from agentd.scheduling.readiness import (
    Readiness,
    barrier_readiness,
    dependency_readiness,
    gang_readiness,
)
from agentd.scheduling.reconnaissance import (
    RECONNAISSANCE_ACCEPTANCE_CRITERIA,
    ReconnaissanceLimits,
    ReconnaissanceOutcome,
    compile_reconnaissance,
    is_promotable,
    promote_hors_categorie,
)
from agentd.scheduling.tail import TailDecision, TailThresholds, evaluate_tail

__all__ = [
    "RECONNAISSANCE_ACCEPTANCE_CRITERIA",
    "Placement",
    "Readiness",
    "ReconnaissanceLimits",
    "ReconnaissanceOutcome",
    "TailDecision",
    "TailThresholds",
    "barrier_readiness",
    "burn_order_key",
    "compatible_placements",
    "compile_reconnaissance",
    "declares_burn_eligibility",
    "dependency_readiness",
    "evaluate_tail",
    "gang_readiness",
    "is_burn_candidate",
    "is_promotable",
    "order_jobs",
    "order_jobs_for_quota_mode",
    "priority_key",
    "promote_hors_categorie",
    "select_placement",
]
