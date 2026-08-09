"""Deterministic tail-governor policy for effort overruns."""

from dataclasses import dataclass

from agentd.domain.enums import TailAction
from agentd.domain.models import EffortEstimate


@dataclass(frozen=True, slots=True)
class TailThresholds:
    """Ratios used when a finite p99 estimate is unavailable."""

    significant_overrun_ratio: float = 1.25
    runaway_ratio: float = 2.0

    def __post_init__(self) -> None:
        if self.significant_overrun_ratio <= 1:
            raise ValueError("Significant-overrun ratio must be greater than one")
        if self.runaway_ratio <= self.significant_overrun_ratio:
            raise ValueError("Runaway ratio must exceed significant-overrun ratio")


@dataclass(frozen=True, slots=True)
class TailDecision:
    action: TailAction
    consumed: float
    p90: float
    checkpoint_after: float
    runaway_after: float


_DEFAULT_THRESHOLDS = TailThresholds()


def evaluate_tail(
    estimate: EffortEstimate,
    consumed: float,
    thresholds: TailThresholds = _DEFAULT_THRESHOLDS,
) -> TailDecision:
    """Select the next tail action without reserving the theoretical p99.

    Values through p90 continue. The first overrun requests re-estimation,
    significant overruns checkpoint and replan, and consumption beyond a finite
    p99 (or the configured unbounded-tail ratio) is converted to
    hors-categorie work.
    """

    if consumed < 0:
        raise ValueError("Consumed effort cannot be negative")

    checkpoint_after = estimate.p90 * thresholds.significant_overrun_ratio
    runaway_after = estimate.p90 * thresholds.runaway_ratio
    if estimate.p99 is not None:
        checkpoint_after = min(checkpoint_after, estimate.p99)
        runaway_after = estimate.p99
    runaway_after = max(checkpoint_after, runaway_after)

    if consumed <= estimate.p90:
        action = TailAction.CONTINUE
    elif consumed <= checkpoint_after:
        action = TailAction.REESTIMATE
    elif consumed <= runaway_after:
        action = TailAction.CHECKPOINT_REPLAN
    else:
        action = TailAction.CONVERT_TO_HORS_CATEGORIE

    return TailDecision(
        action=action,
        consumed=consumed,
        p90=estimate.p90,
        checkpoint_after=checkpoint_after,
        runaway_after=runaway_after,
    )
