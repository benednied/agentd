"""Conservative, read-only provider quota reset detection.

Provider snapshots expose window percentages and reset timestamps, not a
convertible absolute balance.  This module therefore detects only a new window
with a material drop in the observed used fraction.  It never mutates quota
state and never invents an absolute remaining amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import TypedDict

from agentd.domain.enums import QuotaMode
from agentd.domain.models import (
    ProviderQuotaSnapshot,
    QuotaResetEvent,
)
from agentd.runtime.accounts import provider_used_percent


@dataclass(frozen=True, slots=True)
class ProviderResetPolicy:
    """Safety thresholds for automatic reset recognition."""

    snapshot_stale_after: timedelta = timedelta(minutes=5)
    minimum_used_fraction_drop: float = 0.50

    def __post_init__(self) -> None:
        if self.snapshot_stale_after <= timedelta(0):
            raise ValueError("Reset snapshot staleness threshold must be positive")
        if (
            not isfinite(self.minimum_used_fraction_drop)
            or not 0 < self.minimum_used_fraction_drop <= 1
        ):
            raise ValueError("Reset used-fraction drop must be in the interval (0, 1]")


DEFAULT_PROVIDER_RESET_POLICY = ProviderResetPolicy()


class _DecisionBase(TypedDict):
    provider: str
    pool_id: str
    bucket_id: str
    previous_reset_at: datetime | None
    reset_at: datetime | None
    confidence: float


class _DecisionFractions(TypedDict):
    previous_used_fraction: float
    current_used_fraction: float
    used_fraction_drop: float


@dataclass(frozen=True, slots=True)
class ProviderResetDecision:
    """Pure evidence and identity returned by reset detection."""

    confirmed: bool
    provider: str
    pool_id: str
    bucket_id: str
    event_id: str | None = None
    reason: str = ""
    previous_used_fraction: float | None = None
    current_used_fraction: float | None = None
    used_fraction_drop: float | None = None
    previous_reset_at: datetime | None = None
    reset_at: datetime | None = None
    confidence: float = 0

    @property
    def episode_id(self) -> str | None:
        """Compatibility name for the stable reset-event identity."""

        return self.event_id


def _fresh(
    snapshot: ProviderQuotaSnapshot,
    *,
    at: datetime,
    policy: ProviderResetPolicy,
) -> bool:
    age = at - snapshot.observed_at
    return timedelta(0) <= age <= policy.snapshot_stale_after


def _window_marker(
    previous: ProviderQuotaSnapshot, current: ProviderQuotaSnapshot
) -> str | None:
    previous_reset = previous.reset_at
    current_reset = current.reset_at
    moved_forward = (
        previous_reset is not None
        and current_reset is not None
        and current_reset > previous_reset
    )
    previous_expired = (
        previous_reset is not None and current.observed_at > previous_reset
    )
    if not (moved_forward or previous_expired):
        return None
    if current_reset is not None:
        return current_reset.isoformat()
    if previous_reset is not None:
        return f"after:{previous_reset.isoformat()}"
    return None  # pragma: no cover - implied by the evidence predicates above


def _event_identity(current: ProviderQuotaSnapshot, window_marker: str) -> str:
    material = "|".join(
        (current.provider, current.pool_id, current.bucket_id, window_marker)
    )
    return sha256(material.encode("utf-8")).hexdigest()[:32]


def detect_provider_reset(
    previous: ProviderQuotaSnapshot,
    current: ProviderQuotaSnapshot,
    *,
    at: datetime,
    policy: ProviderResetPolicy = DEFAULT_PROVIDER_RESET_POLICY,
) -> ProviderResetDecision:
    """Compare consecutive snapshots without writing or fabricating quota.

    A positive decision requires matching provider identity, strictly newer and
    fresh observations, explicit new-window evidence, and a material decrease
    in the provider-reported used fraction.  Duplicate, stale, incomplete, or
    out-of-order observations remain unconfirmed.
    """

    same_bucket = (
        previous.provider == current.provider
        and previous.pool_id == current.pool_id
        and previous.bucket_id == current.bucket_id
    )
    base: _DecisionBase = {
        "provider": current.provider,
        "pool_id": current.pool_id,
        "bucket_id": current.bucket_id,
        "previous_reset_at": previous.reset_at,
        "reset_at": current.reset_at,
        "confidence": min(previous.confidence, current.confidence),
    }
    if not same_bucket:
        return ProviderResetDecision(
            confirmed=False,
            reason="provider, pool, or bucket changed",
            **base,
        )
    if previous.id == current.id or current.observed_at <= previous.observed_at:
        return ProviderResetDecision(
            confirmed=False,
            reason="duplicate or out-of-order snapshots",
            **base,
        )
    if not _fresh(previous, at=at, policy=policy) or not _fresh(
        current, at=at, policy=policy
    ):
        return ProviderResetDecision(
            confirmed=False,
            reason="reset evidence is stale or from the future",
            **base,
        )

    previous_used = provider_used_percent(previous)
    current_used = provider_used_percent(current)
    if previous_used is None or current_used is None:
        return ProviderResetDecision(
            confirmed=False,
            reason="both snapshots need provider used percentages",
            **base,
        )
    previous_fraction = previous_used / 100
    current_fraction = current_used / 100
    drop = previous_fraction - current_fraction
    fractions: _DecisionFractions = {
        "previous_used_fraction": previous_fraction,
        "current_used_fraction": current_fraction,
        "used_fraction_drop": drop,
    }
    window_marker = _window_marker(previous, current)
    if window_marker is None:
        return ProviderResetDecision(
            confirmed=False,
            reason="no clearly evidenced new provider window",
            **fractions,
            **base,
        )
    if drop < policy.minimum_used_fraction_drop:
        return ProviderResetDecision(
            confirmed=False,
            reason="used fraction did not decrease materially",
            **fractions,
            **base,
        )
    return ProviderResetDecision(
        confirmed=True,
        event_id=_event_identity(current, window_marker),
        reason="fresh new provider window with a material used-fraction decrease",
        **fractions,
        **base,
    )


def reset_event_for_decision(
    decision: ProviderResetDecision,
    *,
    new_remaining: float | None = None,
    source: str = "automatic-provider-reset",
) -> QuotaResetEvent | None:
    """Build a confirmed reset event only from an explicit absolute amount.

    Percentage-only provider telemetry deliberately produces no event.  The
    caller must supply an independently configured absolute ``new_remaining``;
    this helper does not derive it from percentages or token counters.
    """

    if not decision.confirmed or new_remaining is None or decision.event_id is None:
        return None
    if not isfinite(new_remaining) or new_remaining < 0:
        raise ValueError("Configured new_remaining must be finite and non-negative")
    if not source.strip():
        raise ValueError("Reset event source cannot be empty")
    return QuotaResetEvent(
        id=decision.event_id,
        pool_id=decision.pool_id,
        mode=QuotaMode.RESET_CONFIRMED,
        expected_reset_at=decision.reset_at,
        confidence=decision.confidence,
        new_remaining=new_remaining,
        source=source,
    )


def evaluate_provider_reset(
    previous: ProviderQuotaSnapshot,
    current: ProviderQuotaSnapshot,
    *,
    at: datetime,
    policy: ProviderResetPolicy = DEFAULT_PROVIDER_RESET_POLICY,
) -> ProviderResetDecision:
    """Alias emphasizing that detection is a pure evaluation."""

    return detect_provider_reset(previous, current, at=at, policy=policy)


__all__ = [
    "DEFAULT_PROVIDER_RESET_POLICY",
    "ProviderResetDecision",
    "ProviderResetPolicy",
    "detect_provider_reset",
    "evaluate_provider_reset",
    "reset_event_for_decision",
]
