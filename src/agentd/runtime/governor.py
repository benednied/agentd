"""Pure, durable-command policies for provider and effort tail pressure.

The daemon owns polling and the coordinator owns persistence.  This module keeps
the decisions deterministic so retries and process restarts produce the same
command identifiers rather than a stream of duplicate steering requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite

from agentd.domain.enums import TailAction
from agentd.domain.models import (
    Job,
    ProviderQuotaSnapshot,
    RunCommand,
    RunObservation,
    RunRecord,
)
from agentd.runtime.accounts import (
    DEFAULT_ACCOUNT_POLICY,
    AccountPolicyThresholds,
    provider_quota_reached,
    provider_remaining_fraction,
    snapshot_is_stale,
)
from agentd.scheduling.tail import TailDecision, evaluate_tail

PROVIDER_STOP_REMAINING_FRACTION = 0.02


@dataclass(frozen=True, slots=True)
class ProviderStopPolicy:
    """Hard-stop active work at the fixed two-percent provider boundary."""

    remaining_fraction: float = PROVIDER_STOP_REMAINING_FRACTION

    def __post_init__(self) -> None:
        if self.remaining_fraction != PROVIDER_STOP_REMAINING_FRACTION:
            raise ValueError("Provider stop fraction must be exactly 0.02")


@dataclass(frozen=True, slots=True)
class ProviderStopDecision:
    stop: bool
    remaining_fraction: float | None
    episode: str | None
    reason: str | None = None


DEFAULT_PROVIDER_STOP_POLICY = ProviderStopPolicy()


def evaluate_provider_stop(
    snapshot: ProviderQuotaSnapshot,
    *,
    at: datetime,
    policy: ProviderStopPolicy = DEFAULT_PROVIDER_STOP_POLICY,
    account_policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> ProviderStopDecision:
    """Evaluate a provider hard stop without fabricating an absolute balance.

    Stale telemetry cannot prove that the two-percent boundary has been crossed.
    Existing admission policy already treats stale snapshots conservatively; it
    would be unsafe to interrupt active work based on an old percentage.
    """

    if snapshot_is_stale(snapshot, at=at, policy=account_policy):
        return ProviderStopDecision(False, None, None)

    remaining = provider_remaining_fraction(snapshot)
    reached = provider_quota_reached(snapshot)
    stop = reached or (remaining is not None and remaining <= policy.remaining_fraction)
    if not stop:
        return ProviderStopDecision(False, remaining, None)

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
    reason = (
        "provider reported the quota limit as reached"
        if reached
        else "fresh provider telemetry has at most the configured quota tail left"
    )
    return ProviderStopDecision(True, remaining, episode, reason)


def provider_stop_command(
    snapshot: ProviderQuotaSnapshot,
    *,
    run_id: str,
    at: datetime,
    policy: ProviderStopPolicy = DEFAULT_PROVIDER_STOP_POLICY,
    account_policy: AccountPolicyThresholds = DEFAULT_ACCOUNT_POLICY,
) -> RunCommand | None:
    """Return one stable interrupt command per run and provider window."""

    decision = evaluate_provider_stop(
        snapshot,
        at=at,
        policy=policy,
        account_policy=account_policy,
    )
    if not decision.stop or decision.episode is None:
        return None
    return RunCommand(
        id=f"provider-hard-stop:{run_id}:{decision.episode}",
        run_id=run_id,
        action="interrupt",
        payload={
            "reason": decision.reason,
            "provider": snapshot.provider,
            "pool_id": snapshot.pool_id,
            "bucket_id": snapshot.bucket_id,
            "remaining_fraction": decision.remaining_fraction,
            "threshold_fraction": policy.remaining_fraction,
            "reset_at": (
                snapshot.reset_at.isoformat() if snapshot.reset_at is not None else None
            ),
        },
        created_at=at,
    )


def observed_effort(
    job: Job,
    run: RunRecord,
    *,
    at: datetime,
    observation: RunObservation | None = None,
) -> float | None:
    """Resolve effort in the job's declared unit from trusted observation data.

    Drivers may report ``effort_consumed`` together with an exact ``effort_unit``.
    In their absence, wall-clock time is a deterministic fallback only for the
    built-in ``agent-minutes`` unit.  Token usage is deliberately not converted
    into time or effort.
    """

    if observation is not None:
        raw_consumed = observation.metadata.get("effort_consumed")
        raw_unit = observation.metadata.get("effort_unit")
        if (
            isinstance(raw_consumed, int | float)
            and not isinstance(raw_consumed, bool)
            and isinstance(raw_unit, str)
            and raw_unit == job.effort.unit
        ):
            consumed = float(raw_consumed)
            if isfinite(consumed) and consumed >= 0:
                return consumed

    if job.effort.unit != "agent-minutes":
        return None
    elapsed = (at - run.started_at).total_seconds() / 60
    return max(0.0, elapsed)


def tail_governor_command(
    job: Job,
    run: RunRecord,
    *,
    at: datetime,
    observation: RunObservation | None = None,
) -> tuple[TailDecision, RunCommand | None] | None:
    """Map one effort decision to an idempotent durable worker command."""

    consumed = observed_effort(job, run, at=at, observation=observation)
    if consumed is None:
        return None
    decision = evaluate_tail(job.effort, consumed)
    if decision.action is TailAction.CONTINUE:
        return decision, None

    action = "steer" if decision.action is TailAction.REESTIMATE else "checkpoint"
    instructions = {
        TailAction.REESTIMATE: (
            "Re-estimate the remaining work now. Report concrete completed work, "
            "remaining steps, risks, and a revised bounded effort estimate before "
            "continuing."
        ),
        TailAction.CHECKPOINT_REPLAN: (
            "Stop at the next safe boundary and return a durable checkpoint. The "
            "work exceeded its expected tail and must be replanned before resume."
        ),
        TailAction.CONVERT_TO_HORS_CATEGORIE: (
            "Stop at the next safe boundary and return a durable checkpoint. The "
            "work exceeded its bounded tail and requires hors-categorie planning."
        ),
    }
    return decision, RunCommand(
        id=f"tail-governor:{run.id}:{decision.action.value}",
        run_id=run.id,
        action=action,
        payload={
            "reason": "automatic effort tail governor",
            "instruction": instructions[decision.action],
            "tail_action": decision.action.value,
            "effort_unit": job.effort.unit,
            "consumed": decision.consumed,
            "p90": decision.p90,
            "checkpoint_after": decision.checkpoint_after,
            "runaway_after": decision.runaway_after,
        },
        created_at=at,
    )


__all__ = [
    "ProviderStopDecision",
    "ProviderStopPolicy",
    "evaluate_provider_stop",
    "observed_effort",
    "provider_stop_command",
    "tail_governor_command",
]
