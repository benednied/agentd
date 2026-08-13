"""Quota admission, reservation, release, and reset-event handling."""

from dataclasses import replace
from math import isfinite

from agentd.domain.enums import (
    JobState,
    QoSClass,
    QuotaMode,
    QuotaUnit,
    ReservationState,
)
from agentd.domain.models import (
    Job,
    QuotaPool,
    QuotaReservation,
    QuotaResetEvent,
    UsageApplication,
    UsageSample,
    utc_now,
)
from agentd.domain.transitions import transition_job
from agentd.observability import event_logger
from agentd.state.base import ConcurrentStateError, StateStore

_MAX_OPTIMISTIC_ATTEMPTS = 8


class QuotaAdmissionError(RuntimeError):
    """Raised when a job cannot reserve its required schedulable quota."""

    pass


class QuotaMaximumExceeded(RuntimeError):
    """Raised after a sample is durably charged beyond its cumulative maximum."""

    def __init__(self, application: UsageApplication) -> None:
        self.application = application
        super().__init__(
            f"Cumulative usage {application.job_consumed:g} exceeds maximum "
            f"{application.maximum:g}"
        )


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
            if job.quota_budget.unit is not pool.unit:
                raise QuotaAdmissionError(
                    f"Job {job.id} budget unit does not match pool {pool.id}"
                )
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
                unit=pool.unit,
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
            event_logger(
                component="quota",
                operation="reserve",
                job_id=job.id,
                reservation_id=reservation.id,
                pool_id=pool.id,
                unit=pool.unit.value,
            ).info("quota_reserved")
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
            if reservation.state in {
                ReservationState.RELEASED,
                ReservationState.CANCELLED,
            }:
                return reservation
            pool = self._store.get_quota_pool(reservation.pool_id)
            unreserved = pool.reserved - reservation.outstanding
            if unreserved < -1e-9:
                raise ConcurrentStateError(
                    f"Pool {pool.id} reserves less than reservation {reservation.id}"
                )
            state = (
                ReservationState.CANCELLED if cancelled else ReservationState.RELEASED
            )
            now = utc_now()
            final_consumed = max(consumed, reservation.consumed)
            delta = final_consumed - reservation.consumed
            debt_delta = max(0, delta - pool.remaining)
            updated_reservation = replace(
                reservation,
                state=state,
                consumed=final_consumed,
                debt=reservation.debt + debt_delta,
                released_at=now,
            )
            updated_pool = replace(
                pool,
                remaining=max(0, pool.remaining - delta),
                reserved=max(0, unreserved),
                debt=pool.debt + debt_delta,
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
            event_logger(
                component="quota",
                operation="release",
                job_id=reservation.job_id,
                reservation_id=reservation.id,
                pool_id=reservation.pool_id,
                cancelled=cancelled,
            ).info("quota_released")
            return updated_reservation

        raise ConcurrentStateError(
            f"Could not release reservation {reservation_id} after concurrent updates"
        ) from conflict

    def apply_usage(
        self,
        sample: UsageSample,
        *,
        maximum: float | None = None,
    ) -> UsageApplication:
        """Atomically persist and charge one cumulative run usage sample."""

        run = self._store.get_run(sample.run_id)
        job = self._store.get_job(run.job_id)
        effective_maximum = job.quota_budget.maximum if maximum is None else maximum
        return self._store.apply_usage_sample(
            sample,
            maximum=effective_maximum,
        )

    def apply_codex_usage(self, sample: UsageSample) -> UsageApplication:
        """Charge Codex token telemetry and enforce the cumulative job maximum.

        The sample is committed before :class:`QuotaMaximumExceeded` is raised so
        an over-limit turn can never disappear from accounting during shutdown.
        """

        if sample.unit is not QuotaUnit.TOKENS:
            raise ValueError("Codex cumulative usage must use token quota units")
        application = self.apply_usage(sample)
        if application.maximum_exceeded:
            raise QuotaMaximumExceeded(application)
        return application

    def top_up(self, reservation_id: str, amount: float) -> QuotaReservation:
        """Reserve additional future headroom without charging it as usage."""

        reservation = self._store.get_reservation(reservation_id)
        job = self._store.get_job(reservation.job_id)
        pool = self._store.get_quota_pool(reservation.pool_id)
        minimum_dispatchable = (
            0
            if job.qos in {QoSClass.INTERACTIVE, QoSClass.BLOCKER}
            else pool.minimum_interactive_reserve
        )
        return self._store.top_up_quota(
            reservation_id,
            amount,
            minimum_dispatchable=minimum_dispatchable,
        )

    def begin_metering(
        self,
        job_id: str,
        *,
        reason: str = "run quiesced; final usage reconciliation pending",
    ) -> QuotaReservation:
        """Atomically enter the durable job/reservation metering phase."""

        job = self._store.get_job(job_id)
        if job.state not in {
            JobState.RUNNING,
            JobState.DRAINING,
            JobState.CHECKPOINTED,
            JobState.REVIEW,
        }:
            raise ValueError(f"Job {job_id} cannot begin metering from {job.state}")
        reservation = self._store.find_active_reservation(job_id)
        if reservation is None:
            raise LookupError(f"Job {job_id} has no outstanding reservation")
        pending, event = transition_job(job, JobState.METERING_PENDING, reason)
        return self._store.begin_metering(pending, event, reservation.id)

    def settle(
        self,
        reservation_id: str,
        *,
        final_sample: UsageSample | None = None,
        cancelled: bool = False,
        maximum: float | None = None,
    ) -> QuotaReservation:
        """Apply optional final telemetry and release unused reserved headroom."""

        reservation = self._store.get_reservation(reservation_id)
        job = self._store.get_job(reservation.job_id)
        effective_maximum = job.quota_budget.maximum if maximum is None else maximum
        return self._store.settle_quota_usage(
            reservation_id,
            final_sample=final_sample,
            cancelled=cancelled,
            maximum=effective_maximum,
        )

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
            or reservation.unit is not job.quota_budget.unit
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
