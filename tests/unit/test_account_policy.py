from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentd.domain.enums import QoSClass, QuotaMode, ReservationState
from agentd.domain.models import Job, ProviderQuotaSnapshot, QuotaReservation
from agentd.runtime.accounts import (
    hard_cap_interrupt_command,
    hard_cap_reached,
    maximum_checkpoint_command,
    provider_allows_qos,
    provider_checkpoint_command,
    quota_mode_for_snapshot,
    reservation_top_up_amount,
    should_checkpoint_active_run,
    should_checkpoint_for_maximum,
    should_enter_pre_reset_burn,
    should_top_up,
    snapshot_is_stale,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
URGENT = (QoSClass.INTERACTIVE, QoSClass.BLOCKER)
NONURGENT = tuple(qos for qos in QoSClass if qos not in URGENT)


def _snapshot(
    used: float | None,
    *,
    observed_at: datetime = NOW,
    reset_at: datetime | None = None,
    reached: bool = False,
    credits_exhausted: bool | None = None,
) -> ProviderQuotaSnapshot:
    return ProviderQuotaSnapshot(
        id="snapshot",
        pool_id="default",
        bucket_id="codex",
        primary_used_percent=used,
        primary_reset_at=reset_at,
        reached=reached,
        credits_exhausted=credits_exhausted,
        observed_at=observed_at,
    )


@pytest.mark.parametrize("qos", tuple(QoSClass))
def test_provider_policy_below_75_percent_allows_every_qos(qos: QoSClass) -> None:
    assert provider_allows_qos(_snapshot(74.999), qos, at=NOW)


@pytest.mark.parametrize("qos", tuple(QoSClass))
def test_provider_policy_at_75_blocks_only_background(qos: QoSClass) -> None:
    expected = qos not in {QoSClass.SPECULATIVE, QoSClass.SCAVENGER}
    assert provider_allows_qos(_snapshot(75), qos, at=NOW) is expected


@pytest.mark.parametrize("qos", tuple(QoSClass))
def test_provider_policy_at_90_allows_only_urgent(qos: QoSClass) -> None:
    assert provider_allows_qos(_snapshot(90), qos, at=NOW) is (qos in URGENT)
    assert should_checkpoint_active_run(_snapshot(90), qos, at=NOW) is (
        qos in NONURGENT
    )


@pytest.mark.parametrize("exhaustion", ("reached", "credits"))
@pytest.mark.parametrize("qos", tuple(QoSClass))
def test_explicit_provider_exhaustion_blocks_every_qos(
    exhaustion: str,
    qos: QoSClass,
) -> None:
    snapshot = _snapshot(
        10,
        reached=exhaustion == "reached",
        credits_exhausted=True if exhaustion == "credits" else None,
    )
    assert not provider_allows_qos(snapshot, qos, at=NOW)
    assert should_checkpoint_active_run(snapshot, qos, at=NOW)


def test_provider_policy_staleness_boundary_is_strictly_over_five_minutes() -> None:
    exactly_five_minutes_old = _snapshot(
        10,
        observed_at=NOW - timedelta(minutes=5),
    )
    stale = replace(
        exactly_five_minutes_old,
        observed_at=NOW - timedelta(minutes=5, microseconds=1),
    )

    assert not snapshot_is_stale(exactly_five_minutes_old, at=NOW)
    assert provider_allows_qos(exactly_five_minutes_old, QoSClass.NORMAL, at=NOW)
    assert snapshot_is_stale(stale, at=NOW)
    assert not provider_allows_qos(stale, QoSClass.NORMAL, at=NOW)
    assert provider_allows_qos(stale, QoSClass.INTERACTIVE, at=NOW)


def test_pre_reset_burn_requires_fresh_known_usage_below_75_and_reset_within_12h() -> (
    None
):
    eligible = _snapshot(74.999, reset_at=NOW + timedelta(hours=12))

    assert should_enter_pre_reset_burn(eligible, at=NOW)
    assert quota_mode_for_snapshot(eligible, at=NOW) is QuotaMode.PRE_RESET_BURN
    assert not should_enter_pre_reset_burn(
        _snapshot(75, reset_at=NOW + timedelta(hours=12)), at=NOW
    )
    assert not should_enter_pre_reset_burn(
        _snapshot(None, reset_at=NOW + timedelta(hours=1)), at=NOW
    )
    assert not should_enter_pre_reset_burn(
        _snapshot(10, reset_at=NOW + timedelta(hours=12, microseconds=1)),
        at=NOW,
    )
    assert not should_enter_pre_reset_burn(
        _snapshot(
            10,
            observed_at=NOW - timedelta(minutes=5, microseconds=1),
            reset_at=NOW + timedelta(hours=1),
        ),
        at=NOW,
    )


def test_job_usage_policy_boundaries_and_exactly_once_checkpoint_command() -> None:
    reservation = QuotaReservation(
        id="reservation",
        job_id="job",
        pool_id="default",
        amount=100_000,
        state=ReservationState.ACTIVE,
        consumed=79_999,
        created_at=NOW,
    )

    assert not should_top_up(reservation)
    assert reservation_top_up_amount(reservation) == 0
    at_threshold = replace(reservation, consumed=80_000)
    assert should_top_up(at_threshold)
    assert reservation_top_up_amount(at_threshold) == 25_000

    assert not should_checkpoint_for_maximum(89_999, 100_000)
    assert should_checkpoint_for_maximum(90_000, 100_000)
    assert should_checkpoint_for_maximum(100_000, 100_000)
    assert not hard_cap_reached(99_999, 100_000)
    assert hard_cap_reached(100_000, 100_000)

    first = maximum_checkpoint_command(
        job_id="job",
        run_id="run",
        consumed=90_000,
        maximum=100_000,
        at=NOW,
    )
    retry = maximum_checkpoint_command(
        job_id="job",
        run_id="run",
        consumed=95_000,
        maximum=100_000,
        at=NOW + timedelta(seconds=1),
    )
    assert first is not None
    assert retry is not None
    assert first.id == retry.id == "usage-maximum-checkpoint:job"
    assert first.payload == retry.payload


def test_provider_checkpoint_command_is_stable_for_one_quota_window() -> None:
    reset_at = NOW + timedelta(hours=2)
    initial = _snapshot(90, reset_at=reset_at)
    retry_snapshot = replace(
        initial,
        id="snapshot-retry",
        primary_used_percent=99,
        observed_at=NOW + timedelta(minutes=1),
    )

    first = provider_checkpoint_command(
        initial,
        run_id="run",
        qos=QoSClass.NORMAL,
        at=NOW,
    )
    retry = provider_checkpoint_command(
        retry_snapshot,
        run_id="run",
        qos=QoSClass.NORMAL,
        at=NOW + timedelta(minutes=1),
    )
    assert first is not None
    assert retry is not None
    assert first.id == retry.id
    assert first.payload == retry.payload
    assert (
        provider_checkpoint_command(
            initial,
            run_id="urgent-run",
            qos=QoSClass.INTERACTIVE,
            at=NOW,
        )
        is None
    )
    reached = replace(initial, reached=True)
    assert (
        provider_checkpoint_command(
            reached,
            run_id="urgent-run",
            qos=QoSClass.INTERACTIVE,
            at=NOW,
        )
        is not None
    )


def test_hard_cap_interrupt_command_has_stable_id_and_grace_deadline() -> None:
    assert (
        hard_cap_interrupt_command(
            job_id="job",
            run_id="run",
            consumed=99_999,
            maximum=100_000,
            reached_at=NOW,
        )
        is None
    )

    command = hard_cap_interrupt_command(
        job_id="job",
        run_id="run",
        consumed=100_000,
        maximum=100_000,
        reached_at=NOW,
    )
    retry = hard_cap_interrupt_command(
        job_id="job",
        run_id="run",
        consumed=110_000,
        maximum=100_000,
        reached_at=NOW,
    )
    assert command is not None
    assert retry is not None
    assert command.id == retry.id == "usage-hard-cap-interrupt:job"
    assert command.payload == retry.payload
    assert command.payload["deadline"] == (NOW + timedelta(seconds=120)).isoformat()
    assert command.created_at == NOW + timedelta(seconds=120)
    with pytest.raises(ValueError, match="grace"):
        hard_cap_interrupt_command(
            job_id="job",
            run_id="run",
            consumed=100_000,
            maximum=100_000,
            reached_at=NOW,
            grace=timedelta(0),
        )


def test_job_base_ref_is_serialized_and_rejects_option_like_values(
    make_job: Callable[..., Job],
) -> None:
    job = make_job(base_ref="f360813f777df3358a71f889fe195675c1af1ab9")

    assert Job.from_dict(job.to_dict()) == job
    assert replace(job, objective="replacement keeps base").base_ref == job.base_ref
    with pytest.raises(ValueError, match="base ref"):
        make_job(base_ref="")
    with pytest.raises(ValueError, match="base ref"):
        make_job(base_ref="-bad-ref")
    with pytest.raises(ValueError, match="base ref"):
        make_job(base_ref=" -bad-ref")
    with pytest.raises(ValueError, match="base ref"):
        make_job(base_ref="HEAD ")
    with pytest.raises(ValueError, match="base ref"):
        make_job(base_ref="bad\0ref")
