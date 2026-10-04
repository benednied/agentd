"""Administrative local spending windows, independent of provider capacity.

These grants are approved absolute token allowances. Provider percentages still
gate admission separately; neither a clock tick nor this grant proves capacity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import Any

from agentd.domain.enums import QuotaMode
from agentd.domain.models import QuotaPool, QuotaResetEvent
from agentd.state.base import StateStore

LOCAL_ALLOWANCE_SOURCE = "administrative-local-allowance:"
_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LocalAllowancePolicy:
    """Replace unused local allowance once per approved UTC spending window.

    Missed windows do not accumulate. The immutable policy ID also prevents a
    config edit from silently granting a second allowance in an existing window.
    Changing an established policy requires an explicitly new policy identity.
    """

    pool_id: str
    policy_id: str
    tokens_per_window: float
    window_seconds: int = 86_400
    anchor: datetime = _EPOCH

    def __post_init__(self) -> None:
        if not self.pool_id.strip():
            raise ValueError("Local allowance requires a pool ID")
        if _POLICY_ID.fullmatch(self.policy_id) is None:
            raise ValueError("Local allowance policy ID must be a compact stable name")
        if not isfinite(self.tokens_per_window) or self.tokens_per_window <= 0:
            raise ValueError("Local allowance must be finite and positive")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, int)
            or self.window_seconds <= 0
        ):
            raise ValueError(
                "Local allowance window_seconds must be a positive integer"
            )
        if self.anchor.utcoffset() != timedelta(0):
            raise ValueError("Local allowance anchor must use UTC")

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], *, pool_id: str
    ) -> LocalAllowancePolicy:
        allowed = {"policy_id", "tokens_per_window", "window_seconds", "anchor"}
        if set(config) - allowed:
            raise ValueError("Unknown local allowance policy fields")
        return cls(
            pool_id=pool_id,
            policy_id=str(config["policy_id"]),
            tokens_per_window=float(config["tokens_per_window"]),
            window_seconds=config.get("window_seconds", 86_400),
            anchor=datetime.fromisoformat(str(config["anchor"]))
            if "anchor" in config
            else _EPOCH,
        )

    def event_at(self, at: datetime) -> QuotaResetEvent | None:
        """Return a deterministic grant for the current window, never arrears."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Local allowance observations must be timezone aware")
        elapsed = (at - self.anchor).total_seconds()
        if elapsed < 0:
            return None
        window = int(elapsed // self.window_seconds)
        boundary = self.anchor + timedelta(seconds=(window + 1) * self.window_seconds)
        material = "|".join(
            (
                self.pool_id,
                self.policy_id,
                str(float(self.tokens_per_window)),
                str(self.window_seconds),
                self.anchor.isoformat(),
            )
        )
        digest = sha256(material.encode()).hexdigest()
        identity = sha256(
            f"{self.pool_id}|{self.policy_id}|{window}".encode()
        ).hexdigest()
        return QuotaResetEvent(
            id="local-allowance:" + identity,
            pool_id=self.pool_id,
            mode=QuotaMode.RESET_CONFIRMED,
            expected_reset_at=boundary,
            confidence=1,
            new_remaining=self.tokens_per_window,
            source=LOCAL_ALLOWANCE_SOURCE + self.policy_id + ":" + digest,
        )

    def reconcile(self, store: StateStore, *, at: datetime) -> QuotaPool | None:
        event = self.event_at(at)
        return store.apply_local_allowance(event) if event is not None else None


@dataclass(frozen=True, slots=True)
class CheckpointBudgetPolicy:
    """Bound cumulative job budget growth rather than replenishing job usage."""

    maximum_tokens: float
    increment_tokens: float
    checkpoint_fraction: float = 0.9

    def __post_init__(self) -> None:
        if any(
            not isfinite(value) or value <= 0
            for value in (self.maximum_tokens, self.increment_tokens)
        ):
            raise ValueError("Checkpoint budget amounts must be finite and positive")
        if (
            not isfinite(self.checkpoint_fraction)
            or not 0 < self.checkpoint_fraction < 1
        ):
            raise ValueError("Checkpoint budget fraction must be between zero and one")

    def replanned_maximum(self, current: float, consumed: float) -> float | None:
        """Return sufficient bounded new headroom, or leave the existing cap intact."""
        if (
            not isfinite(current)
            or current <= 0
            or not isfinite(consumed)
            or consumed < 0
        ):
            raise ValueError("Checkpoint budget requires valid maximum and usage")
        if consumed < current * self.checkpoint_fraction:
            return None
        next_maximum = min(self.maximum_tokens, current + self.increment_tokens)
        if (
            next_maximum <= current
            or consumed >= next_maximum * self.checkpoint_fraction
        ):
            return None
        return next_maximum
