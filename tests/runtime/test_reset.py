from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import QuotaMode
from agentd.domain.models import ProviderQuotaSnapshot
from agentd.runtime.reset import (
    ProviderResetPolicy,
    detect_provider_reset,
    reset_event_for_decision,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _snapshot(
    snapshot_id: str,
    *,
    observed_at: datetime,
    used: float | None,
    reset_at: datetime | None,
    provider: str = "provider",
    pool_id: str = "pool",
    bucket_id: str = "window",
) -> ProviderQuotaSnapshot:
    return ProviderQuotaSnapshot(
        id=snapshot_id,
        provider=provider,
        pool_id=pool_id,
        bucket_id=bucket_id,
        primary_used_percent=used,
        primary_reset_at=reset_at,
        observed_at=observed_at,
    )


def test_reset_requires_a_new_window_and_material_drop() -> None:
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=90,
        reset_at=NOW + timedelta(hours=1),
    )
    current = _snapshot(
        "after",
        observed_at=NOW + timedelta(minutes=1),
        used=20,
        reset_at=NOW + timedelta(hours=2),
    )

    decision = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))

    assert decision.confirmed is True
    assert decision.previous_used_fraction == pytest.approx(0.9)
    assert decision.current_used_fraction == pytest.approx(0.2)
    assert decision.used_fraction_drop == pytest.approx(0.7)
    assert decision.event_id is not None
    assert decision.event_id == decision.episode_id
    replay = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))
    assert replay.event_id == decision.event_id


def test_reset_can_use_expired_previous_window_at_strict_boundary() -> None:
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=80,
        reset_at=NOW + timedelta(minutes=1),
    )
    current = _snapshot(
        "after",
        observed_at=NOW + timedelta(minutes=1, seconds=1),
        used=10,
        reset_at=None,
    )

    decision = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))

    assert decision.confirmed is True
    assert decision.reset_at is None


def test_reset_requires_crossing_not_exact_reset_timestamp() -> None:
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=80,
        reset_at=NOW + timedelta(minutes=1),
    )
    current = _snapshot(
        "after",
        observed_at=NOW + timedelta(minutes=1),
        used=10,
        reset_at=None,
    )

    decision = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))

    assert decision.confirmed is False
    assert "new provider window" in decision.reason


@pytest.mark.parametrize(
    "mutate",
    (
        "stale",
        "future",
        "duplicate",
        "bucket",
        "no_window",
        "small_drop",
        "missing_usage",
    ),
)
def test_reset_rejects_stale_duplicate_bucket_and_weak_evidence(mutate: str) -> None:
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=90,
        reset_at=NOW + timedelta(hours=1),
    )
    current_kwargs: dict[str, object] = {
        "snapshot_id": "after",
        "observed_at": NOW + timedelta(minutes=1),
        "used": 20,
        "reset_at": NOW + timedelta(hours=2),
    }
    expected_at = NOW + timedelta(minutes=2)
    if mutate == "stale":
        current_kwargs["observed_at"] = NOW - timedelta(minutes=6)
    elif mutate == "future":
        current_kwargs["observed_at"] = NOW + timedelta(minutes=3)
        expected_at = NOW + timedelta(minutes=2)
    elif mutate == "duplicate":
        current_kwargs["snapshot_id"] = "before"
    elif mutate == "bucket":
        current_kwargs["bucket_id"] = "other-window"
    elif mutate == "no_window":
        current_kwargs["reset_at"] = NOW + timedelta(hours=1)
    elif mutate == "small_drop":
        current_kwargs["used"] = 50
    elif mutate == "missing_usage":
        current_kwargs["used"] = None
    current = _snapshot(**current_kwargs)

    decision = detect_provider_reset(previous, current, at=expected_at)

    assert decision.confirmed is False
    assert decision.event_id is None


def test_reset_freshness_and_drop_boundaries_are_explicit() -> None:
    policy = ProviderResetPolicy(
        snapshot_stale_after=timedelta(minutes=5),
        minimum_used_fraction_drop=0.5,
    )
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=80,
        reset_at=NOW + timedelta(minutes=1),
    )
    current = _snapshot(
        "after",
        observed_at=NOW + timedelta(minutes=2),
        used=30,
        reset_at=NOW + timedelta(hours=1),
    )
    exact = detect_provider_reset(
        previous, current, at=NOW + timedelta(minutes=5), policy=policy
    )
    assert exact.confirmed is True

    below = detect_provider_reset(
        previous,
        _snapshot(
            "after-below",
            observed_at=NOW + timedelta(minutes=2),
            used=30.1,
            reset_at=NOW + timedelta(hours=1),
        ),
        at=NOW + timedelta(minutes=2),
        policy=policy,
    )
    assert below.confirmed is False


def test_percentage_only_detection_does_not_create_an_absolute_event() -> None:
    previous = _snapshot(
        "before",
        observed_at=NOW,
        used=90,
        reset_at=NOW + timedelta(hours=1),
    )
    current = _snapshot(
        "after",
        observed_at=NOW + timedelta(minutes=1),
        used=10,
        reset_at=NOW + timedelta(hours=2),
    )
    decision = detect_provider_reset(previous, current, at=NOW + timedelta(minutes=2))

    assert reset_event_for_decision(decision) is None
    event = reset_event_for_decision(decision, new_remaining=123, source="configured")
    assert event is not None
    assert event.mode is QuotaMode.RESET_CONFIRMED
    assert event.pool_id == "pool"
    assert event.new_remaining == 123
    assert event.source == "configured"
    with pytest.raises(ValueError, match="finite"):
        reset_event_for_decision(decision, new_remaining=float("nan"))
