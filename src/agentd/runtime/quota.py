"""Quota admission, reservation, release, and reset-event handling."""

from dataclasses import replace
from math import isfinite

from agentd.domain.enums import QoSClass, QuotaMode, ReservationState
from agentd.domain.models import (
    Job,
    QuotaPool,
    QuotaReservation,
    QuotaResetEvent,
    utc_now,
)
from agentd.state.base import ConcurrentStateError, StateStore

_MAX_OPTIMISTIC_ATTEMPTS = 8


class QuotaAdmissionError(RuntimeError):
    pass


class QuotaManager:
    """Treat provider or subscription quota as a schedulable resource."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def reserve(self, job: Job) -> QuotaReservation:
        required = job.quota_budget.expected_path
        if not isfinite(required):
            raise QuotaAdmissionError("Required quota must be finite")

        conflict: ConcurrentStateError | None = None
        for _attempt in range(_MAX_OPTIMISTIC_ATTEMPTS):
            existing = self._store.find_active_reservation(job.id)
            if existing is not None:
                self._validate_existing(job, required, existing)
                return existing

            pool = self._store.get_quota_pool(job.quota_budget.pool_id)
            available = self.available_for(job, pool)
            if pool.mode == QuotaMode.EMERGENCY_CONSERVE and job.qos not in {
                QoSClass.INTERACTIVE,
                QoSClass.BLOCKER,
            }:
                raise QuotaAdmissionError(
                    f"Pool {pool.id} is conserving quota for urgent jobs"
                )
            if required > available:
                raise QuotaAdmissionError(
                    f"Job {job.id} needs {required:g} quota for an accepted artifact; "
                    f"only {available:g} is dispatchable"
                )
            reservation = QuotaReservation(
                job_id=job.id,
                pool_id=pool.id,
                amount=required,
            )
            updated_pool = replace(
                pool,
                reserved=pool.reserved + required,
                updated_at=utc_now(),
            )
            try:
                self._store.reserve_quota(pool, updated_pool, reservation)
            except ConcurrentStateError as error:
                conflict = error
                continue
            return reservation

        raise ConcurrentStateError(
            f"Could not reserve quota for job {job.id} after concurrent updates"
        ) from conflict

    @staticmethod
    def available_for(job: Job, pool: QuotaPool) -> float:
        available = pool.dispatchable
        if job.qos not in {QoSClass.INTERACTIVE, QoSClass.BLOCKER}:
            available -= pool.minimum_interactive_reserve
        return max(0, available)

    def release(
        self,
        reservation_id: str,
        *,
        consumed: float = 0,
        cancelled: bool = False,
    ) -> QuotaReservation:
        if consumed < 0 or not isfinite(consumed):
            raise ValueError("Consumed quota must be finite and non-negative")

        conflict: ConcurrentStateError | None = None
        for _attempt in range(_MAX_OPTIMISTIC_ATTEMPTS):
            reservation = self._store.get_reservation(reservation_id)
            if reservation.state != ReservationState.ACTIVE:
                return reservation
            pool = self._store.get_quota_pool(reservation.pool_id)
            unreserved = pool.reserved - reservation.amount
            if unreserved < -1e-9:
                raise ConcurrentStateError(
                    f"Pool {pool.id} reserves less than reservation {reservation.id}"
                )
            state = (
                ReservationState.CANCELLED if cancelled else ReservationState.RELEASED
            )
            now = utc_now()
            updated_reservation = replace(
                reservation,
                state=state,
                consumed=consumed,
                released_at=now,
            )
            updated_pool = replace(
                pool,
                remaining=max(0, pool.remaining - consumed),
                reserved=max(0, unreserved),
                updated_at=now,
            )
            try:
                self._store.release_quota(
                    pool,
                    updated_pool,
                    reservation,
                    updated_reservation,
                )
            except ConcurrentStateError as error:
                conflict = error
                continue
            return updated_reservation

        raise ConcurrentStateError(
            f"Could not release reservation {reservation_id} after concurrent updates"
        ) from conflict

    @staticmethod
    def _validate_existing(
        job: Job,
        required: float,
        reservation: QuotaReservation,
    ) -> None:
        if (
            reservation.job_id != job.id
            or reservation.pool_id != job.quota_budget.pool_id
            or reservation.amount != required
        ):
            raise QuotaAdmissionError(
                f"Job {job.id} already has an incompatible active reservation"
            )

    def register_reset_event(self, event: QuotaResetEvent) -> QuotaPool:
        if not 0 <= event.confidence <= 1:
            raise ValueError("Reset confidence must be between zero and one")
        if event.mode == QuotaMode.RESET_CONFIRMED:
            if event.new_remaining is None:
                raise ValueError("A confirmed reset must include the new quota amount")
            if event.new_remaining < 0:
                raise ValueError("Reset quota cannot be negative")

        conflict: ConcurrentStateError | None = None
        for _attempt in range(_MAX_OPTIMISTIC_ATTEMPTS):
            pool = self._store.get_quota_pool(event.pool_id)
            remaining = pool.remaining
            reset_at = event.expected_reset_at
            confidence = event.confidence
            if event.mode == QuotaMode.RESET_CONFIRMED:
                remaining = event.new_remaining
                reset_at = None
                confidence = 1
            updated = replace(
                pool,
                mode=event.mode,
                remaining=remaining,
                reset_at=reset_at,
                reset_confidence=confidence,
                updated_at=utc_now(),
            )
            try:
                self._store.update_quota_pool(pool, updated)
            except ConcurrentStateError as error:
                conflict = error
                continue
            return updated

        raise ConcurrentStateError(
            f"Could not register reset event for pool {event.pool_id} "
            "after concurrent updates"
        ) from conflict
