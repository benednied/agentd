"""Bounded retry accounting from retained, authenticated coding results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agentd.domain.enums import RunOutcome, RunState
from agentd.domain.models import (
    CodingOperation,
    Job,
    QuotaReservation,
    RunRecord,
    TokenUsage,
)


def is_proven_preparation_failure(run: RunRecord) -> bool:
    """Exclude only a stopped remote run that proves no provider work occurred.

    Results in the controller's run ledger arrive through authenticated worker
    lifecycle transport. Missing or contradictory proof remains a coding
    attempt, including failures with zero reported tokens after provider start.
    """
    result = run.result
    return (
        isinstance(run.contract.operation, CodingOperation)
        and run.driver == "remote-coding"
        and run.backend != "direct"
        and run.state is RunState.FAILED
        and result is not None
        and result.outcome is RunOutcome.FAILED
        and result.metadata.get("preparation_failure") is True
        and result.metadata.get("provider_started") is False
        and result.metadata.get("telemetry_valid") is True
        and result.usage == TokenUsage()
        and result.consumed_quota == 0
    )


@dataclass(frozen=True, slots=True)
class CodingAttemptCounts:
    coding: int
    preparation: int
    total: int


@dataclass(frozen=True, slots=True)
class CodingAttemptLimits:
    maximum_coding_attempts: int = 3
    maximum_preparation_attempts: int = 3
    maximum_total_attempts: int | None = None

    def __post_init__(self) -> None:
        for maximum in (
            self.maximum_coding_attempts,
            self.maximum_preparation_attempts,
        ):
            if (
                not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or not 1 <= maximum <= 100
            ):
                raise ValueError("Coding and preparation attempts must be bounded")
        total = self.maximum_total_attempts
        if total is None:
            total = self.maximum_coding_attempts + self.maximum_preparation_attempts
            object.__setattr__(self, "maximum_total_attempts", total)
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or not 1 <= total <= 200
        ):
            raise ValueError("Total coding attempts must be bounded")

    def count(self, runs: Iterable[RunRecord]) -> CodingAttemptCounts:
        total = preparation = 0
        for run in runs:
            total += 1
            preparation += is_proven_preparation_failure(run)
        return CodingAttemptCounts(total - preparation, preparation, total)

    def blocked_reason(
        self,
        runs: Iterable[RunRecord],
        *,
        preparation_retry: bool = False,
    ) -> str | None:
        counts = self.count(runs)
        assert self.maximum_total_attempts is not None
        if counts.total >= self.maximum_total_attempts:
            return "Coding repair total attempt limit has been reached"
        if counts.coding >= self.maximum_coding_attempts:
            return "Coding repair provider attempt limit has been reached"
        if (
            preparation_retry
            and counts.preparation >= self.maximum_preparation_attempts
        ):
            return "Coding repair preparation attempt limit has been reached"
        return None


def current_attempts(job: Job, runs: Iterable[RunRecord]) -> list[RunRecord]:
    """Count only the current operator-authorized budget cycle."""
    retired = (
        job.operation.work_order.retired_run_ids
        if isinstance(job.operation, CodingOperation)
        else ()
    )
    return [run for run in runs if run.id not in retired]


def current_consumed(job: Job, reservations: Iterable[QuotaReservation]) -> float:
    """Old charges remain in the account ledger, outside a fresh job allowance."""
    retired = (
        job.operation.work_order.retired_reservation_ids
        if isinstance(job.operation, CodingOperation)
        else ()
    )
    return sum(item.consumed for item in reservations if item.id not in retired)
