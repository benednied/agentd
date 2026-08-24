"""Pure provider-account and cumulative-job usage policy helpers."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from math import isfinite

from agentd.domain.enums import QoSClass, QuotaMode, ReservationState
from agentd.domain.models import (
    ProviderQuotaSnapshot,
    QuotaReservation,
    RunCommand,
    TokenUsage,
    utc_now,
)

_URGENT_QOS = frozenset({QoSClass.INTERACTIVE, QoSClass.BLOCKER})
_BACKGROUND_QOS = frozenset({QoSClass.SPECULATIVE, QoSClass.SCAVENGER})


@dataclass(frozen=True, slots=True)
class AccountPolicyThresholds:
    """Approved ChatGPT-account admission and reset-burn thresholds."""

    background_block_used_percent: float = 75
    urgent_only_used_percent: float = 90
    snapshot_stale_after: timedelta = timedelta(minutes=5)
    pre_reset_burn_window: timedelta = timedelta(hours=12)

    def __post_init__(self) -> None:
        percentages = (
            self.background_block_used_percent,
            self.urgent_only_used_percent,
        )
        if any(not isfinite(value) or not 0 <= value <= 100 for value in percentages):
            raise ValueError("Account percentages must be between zero and 100")
        if self.background_block_used_percent >= self.urgent_only_used_percent:
            raise ValueError("Background block must precede urgent-only mode")
        if self.snapshot_stale_after <= timedelta(0):
            raise ValueError("Snapshot staleness threshold must be positive")
        if self.pre_reset_burn_window <= timedelta(0):
            raise ValueError("Pre-reset burn window must be positive")


@dataclass(frozen=True, slots=True)
class JobUsagePolicy:
    """Approved reservation and cumulative job-maximum thresholds."""

    top_up_at_fraction: float = 0.80
    top_up_chunk: float = 25_000
    checkpoint_at_fraction: float = 0.90
    hard_cap_at_fraction: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.top_up_at_fraction,
            self.checkpoint_at_fraction,
            self.hard_cap_at_fraction,
        )
        if any(not isfinite(value) or value <= 0 for value in values):
            raise ValueError("Job usage fractions must be finite and positive")
        if not (
            self.top_up_at_fraction
            < self.checkpoint_at_fraction
            < self.hard_cap_at_fraction
        ):
            raise ValueError("Job usage thresholds must increase monotonically")
        if not isfinite(self.top_up_chunk) or self.top_up_chunk <= 0:
            raise ValueError("Top-up chunk must be finite and positive")


DEFAULT_ACCOUNT_POLICY = AccountPolicyThresholds()
DEFAULT_JOB_USAGE_POLICY = JobUsagePolicy()


def codex_cumulative_quota(usage: TokenUsage) -> float:
    """Normalize cumulative Codex input/output tokens to token quota units."""

    return float(usage.total_tokens)


def provider_used_percent(snapshot: ProviderQuotaSnapshot) -> float | None:
    """Return the most constrained provider window's used percentage."""

    values = tuple(
        value
        for value in (
            snapshot.primary_used_percent,
            snapshot.secondary_used_percent,
        )
        if value is not None
    )
    return max(values) if values else None


def provider_remaining_fraction(snapshot: ProviderQuotaSnapshot) -> float | None:
    used = provider_used_percent(snapshot)
    return max(0, 100 - used) / 100 if used is not None else None


def provider_quota_reached(snapshot: ProviderQuotaSnapshot) -> bool:
    return (
        snapshot.reached
        or snapshot.rate_limit_reached_type is not None
        or snapshot.credits_exhausted is True
    )


def snapshot_is_stale(
    snapshot: ProviderQuotaSnapshot,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> bool:
    now = at or utc_now()
    return now - snapshot.observed_at > policy.snapshot_stale_after


def provider_allows_qos(
    snapshot: ProviderQuotaSnapshot,
    qos: QoSClass,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> bool:
    """Apply the provider account gate without converting credits to quota."""

    if provider_quota_reached(snapshot):
        return False
    if snapshot_is_stale(snapshot, at=at, policy=policy):
        return qos in _URGENT_QOS
    used = provider_used_percent(snapshot)
    if used is None or used < policy.background_block_used_percent:
        return True
    if used >= policy.urgent_only_used_percent:
        return qos in _URGENT_QOS
    return qos not in _BACKGROUND_QOS


def should_checkpoint_active_run(
    snapshot: ProviderQuotaSnapshot,
    qos: QoSClass,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> bool:
    """Return whether provider pressure requires an active run to checkpoint.

    A reached limit checkpoints every active run. Before the limit is reached,
    the 90% boundary checkpoints only non-urgent work.
    """

    if provider_quota_reached(snapshot):
        return True
    if qos in _URGENT_QOS:
        return False
    if snapshot_is_stale(snapshot, at=at, policy=policy):
        return False
    used = provider_used_percent(snapshot)
    return used is not None and used >= policy.urgent_only_used_percent


def should_checkpoint_active_nonurgent(
    snapshot: ProviderQuotaSnapshot,
    qos: QoSClass,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> bool:
    """Compatibility alias for :func:`should_checkpoint_active_run`."""

    return should_checkpoint_active_run(snapshot, qos, at=at, policy=policy)


def should_enter_pre_reset_burn(
    snapshot: ProviderQuotaSnapshot,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> bool:
    now = at or utc_now()
    reset_at = snapshot.reset_at
    used = provider_used_percent(snapshot)
    if (
        reset_at is None
        or provider_quota_reached(snapshot)
        or snapshot_is_stale(snapshot, at=now, policy=policy)
        or used is None
        or used >= policy.background_block_used_percent
    ):
        return False
    until_reset = reset_at - now
    return timedelta(0) < until_reset <= policy.pre_reset_burn_window


def quota_mode_for_snapshot(
    snapshot: ProviderQuotaSnapshot,
    *,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> QuotaMode:
    now = at or utc_now()
    used = provider_used_percent(snapshot)
    if provider_quota_reached(snapshot):
        return QuotaMode.EMERGENCY_CONSERVE
    if snapshot_is_stale(snapshot, at=now, policy=policy):
        return QuotaMode.EMERGENCY_CONSERVE
    if used is not None and used >= policy.urgent_only_used_percent:
        return QuotaMode.EMERGENCY_CONSERVE
    if should_enter_pre_reset_burn(snapshot, at=now, policy=policy):
        return QuotaMode.PRE_RESET_BURN
    if snapshot.reset_at is not None:
        return QuotaMode.RESET_ANNOUNCED
    return QuotaMode.NORMAL


def should_top_up(
    reservation: QuotaReservation,
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> bool:
    return (
        reservation.state is ReservationState.ACTIVE
        and reservation.amount > 0
        and reservation.consumed / reservation.amount >= policy.top_up_at_fraction
    )


def reservation_top_up_amount(
    reservation: QuotaReservation,
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> float:
    return policy.top_up_chunk if should_top_up(reservation, policy) else 0


def should_checkpoint_for_maximum(
    consumed: float,
    maximum: float | None,
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> bool:
    if maximum is None:
        return False
    _validate_cumulative_usage(consumed, maximum)
    fraction = consumed / maximum if maximum else 1
    return fraction >= policy.checkpoint_at_fraction


def hard_cap_reached(
    consumed: float,
    maximum: float | None,
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> bool:
    if maximum is None:
        return False
    _validate_cumulative_usage(consumed, maximum)
    fraction = consumed / maximum if maximum else 1
    return fraction >= policy.hard_cap_at_fraction


def maximum_checkpoint_command(
    *,
    job_id: str,
    run_id: str,
    consumed: float,
    maximum: float | None,
    at: datetime | None = None,
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> RunCommand | None:
    """Build one deterministic command ID for the job's 90% checkpoint."""

    if not should_checkpoint_for_maximum(consumed, maximum, policy):
        return None
    return RunCommand(
        id=f"usage-maximum-checkpoint:{job_id}",
        run_id=run_id,
        action="checkpoint",
        payload={
            "reason": "cumulative quota reached 90% of the job maximum",
            "maximum": maximum,
            "threshold_fraction": policy.checkpoint_at_fraction,
        },
        created_at=at or utc_now(),
    )


def provider_checkpoint_command(
    snapshot: ProviderQuotaSnapshot,
    *,
    run_id: str,
    qos: QoSClass,
    at: datetime | None = None,
    policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> RunCommand | None:
    """Build one checkpoint command per run and provider quota window."""

    if not should_checkpoint_active_run(snapshot, qos, at=at, policy=policy):
        return None
    reset_at = snapshot.reset_at
    window_key = "|".join(
        (
            snapshot.provider,
            snapshot.pool_id,
            snapshot.bucket_id,
            reset_at.isoformat() if reset_at is not None else "unknown-window",
        )
    )
    episode = sha256(window_key.encode()).hexdigest()[:20]
    return RunCommand(
        id=f"provider-quota-checkpoint:{run_id}:{episode}",
        run_id=run_id,
        action="checkpoint",
        payload={
            "reason": "provider quota requires a durable checkpoint",
            "provider": snapshot.provider,
            "pool_id": snapshot.pool_id,
            "bucket_id": snapshot.bucket_id,
            "reset_at": reset_at.isoformat() if reset_at is not None else None,
        },
        created_at=at or utc_now(),
    )


def hard_cap_interrupt_command(
    *,
    job_id: str,
    run_id: str,
    consumed: float,
    maximum: float | None,
    reached_at: datetime,
    grace: timedelta = timedelta(seconds=120),
    policy: JobUsagePolicy = DEFAULT_JOB_USAGE_POLICY,
) -> RunCommand | None:
    """Build the job-scoped interrupt due after a hard-cap grace period.

    ``reached_at`` is the durable timestamp of the first sample that reached the
    cap, which keeps the command deadline stable across daemon retries.
    """

    if grace <= timedelta(0):
        raise ValueError("Hard-cap grace must be positive")
    if not hard_cap_reached(consumed, maximum, policy):
        return None
    deadline = reached_at + grace
    return RunCommand(
        id=f"usage-hard-cap-interrupt:{job_id}",
        run_id=run_id,
        action="interrupt",
        payload={
            "reason": "cumulative quota reached the job hard cap",
            "maximum": maximum,
            "deadline": deadline.isoformat(),
        },
        created_at=deadline,
    )


def _validate_cumulative_usage(consumed: float, maximum: float) -> None:
    if not isfinite(consumed) or consumed < 0:
        raise ValueError("Consumed quota must be finite and non-negative")
    if not isfinite(maximum) or maximum < 0:
        raise ValueError("Quota maximum must be finite and non-negative")
