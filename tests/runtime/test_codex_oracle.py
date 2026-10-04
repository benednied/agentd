import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agentd.codex_versions import PINNED_OPENAI_CODEX_VERSION
from agentd.domain.models import JsonValue, ProviderQuotaSnapshot
from agentd.harness.app_server import AppServerEvent, AppServerMetadata
from agentd.runtime.codex_oracle import CodexAccountOracle


@dataclass(slots=True)
class ScriptedAccountClient:
    response: dict[str, JsonValue]
    started: bool = False
    closed: bool = False

    @property
    def metadata(self) -> AppServerMetadata:
        return AppServerMetadata(
            sdk_version=PINNED_OPENAI_CODEX_VERSION,
            runtime_version=PINNED_OPENAI_CODEX_VERSION,
            server_name="codex-app-server",
        )

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def account_rate_limits(self) -> dict[str, JsonValue]:
        return self.response

    async def start_thread(self, *, cwd: str, model: str) -> str:
        raise AssertionError("oracle must not start a thread")

    async def resume_thread(self, thread_id: str, *, cwd: str, model: str) -> str:
        raise AssertionError("oracle must not resume a thread")

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        model: str,
        effort: str,
        output_schema: Mapping[str, JsonValue],
    ) -> str:
        raise AssertionError("oracle must not start a turn")

    async def events(self, turn_id: str) -> AsyncIterator[AppServerEvent]:
        raise AssertionError("oracle must not consume turn events")
        yield

    async def steer(self, thread_id: str, turn_id: str, instruction: str) -> None:
        raise AssertionError("oracle must not steer")

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        raise AssertionError("oracle must not interrupt")


@dataclass(slots=True)
class RecordingSnapshotStore:
    snapshots: list[ProviderQuotaSnapshot] = field(default_factory=list)

    def append_provider_quota_snapshot(self, snapshot: ProviderQuotaSnapshot) -> None:
        self.snapshots.append(snapshot)


def test_codex_oracle_preserves_rate_limit_windows_and_opaque_credits() -> None:
    client = ScriptedAccountClient(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 15,
                    "resetsAt": 1_735_689_600,
                },
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {
                        "usedPercent": 31,
                        "windowDurationMins": 300,
                        "resetsAt": 1_735_689_600,
                    },
                    "secondary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_736_294_400,
                    },
                    "rateLimitReachedType": "secondary",
                    "credits": {
                        "balance": "4.5",
                        "hasCredits": True,
                        "unlimited": False,
                    },
                }
            },
            "rateLimitResetCredits": {"availableCount": 2, "credits": None},
        }
    )
    store = RecordingSnapshotStore()
    oracle = CodexAccountOracle(
        "subscription",
        client_factory=lambda: client,
        store=store,
    )

    snapshot = asyncio.run(oracle.snapshot())

    assert client.started and client.closed
    assert store.snapshots == [snapshot]
    assert snapshot.bucket_id == "codex"
    assert snapshot.primary_used_percent == 31
    assert snapshot.primary_window_minutes == 300
    assert snapshot.primary_reset_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert snapshot.secondary_used_percent == 100
    assert snapshot.reached
    assert snapshot.rate_limit_reached_type == "secondary"
    assert snapshot.plan_type == "pro"
    assert snapshot.credits == {
        "balance": "4.5",
        "hasCredits": True,
        "unlimited": False,
    }
    assert snapshot.credits_exhausted is False
    assert snapshot.rate_limit_reset_credits == {
        "availableCount": 2,
        "credits": None,
    }
    assert snapshot.metadata == {
        "sdk_version": PINNED_OPENAI_CODEX_VERSION,
        "runtime_version": PINNED_OPENAI_CODEX_VERSION,
        "reported_bucket_id": "codex",
        "reported_windows": ["primary", "secondary"],
    }


def test_codex_oracle_rejects_an_unreported_bucket_and_closes_client() -> None:
    client = ScriptedAccountClient(
        {
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 1}},
            "rateLimitsByLimitId": {},
        }
    )
    oracle = CodexAccountOracle(
        "subscription",
        bucket_id="codex-other",
        client_factory=lambda: client,
    )

    async def scenario() -> None:
        try:
            await oracle.snapshot()
        except ValueError as error:
            assert "codex-other" in str(error)
        else:
            raise AssertionError("missing bucket was accepted")

    asyncio.run(scenario())
    assert client.closed


def test_codex_oracle_selects_most_restrictive_reported_bucket() -> None:
    client = ScriptedAccountClient(
        {
            "rateLimitsByLimitId": {
                "codex-hourly": {
                    "limitId": "codex-hourly",
                    "primary": {"usedPercent": 99},
                    "credits": {"hasCredits": True},
                },
                "codex-exhausted": {
                    "limitId": "codex-exhausted",
                    "primary": {"usedPercent": 10},
                    "credits": {"hasCredits": False},
                },
                "codex-weekly": {
                    "limitId": "codex-weekly",
                    "primary": {"usedPercent": 90},
                    "credits": {"hasCredits": True},
                },
            }
        }
    )

    snapshot = asyncio.run(
        CodexAccountOracle(
            "subscription",
            client_factory=lambda: client,
        ).snapshot()
    )

    assert snapshot.bucket_id == "codex-exhausted"
    assert snapshot.primary_used_percent == 10
    assert snapshot.credits_exhausted is True


def test_codex_oracle_explicit_bucket_overrides_automatic_selection() -> None:
    client = ScriptedAccountClient(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 25},
                },
                "codex-weekly": {
                    "limitId": "codex-weekly",
                    "primary": {"usedPercent": 90},
                },
            }
        }
    )

    snapshot = asyncio.run(
        CodexAccountOracle(
            "subscription",
            bucket_id="codex",
            client_factory=lambda: client,
        ).snapshot()
    )

    assert snapshot.bucket_id == "codex"
    assert snapshot.primary_used_percent == 25


def test_codex_oracle_marks_explicit_credit_exhaustion() -> None:
    for credits in (
        {"balance": None, "hasCredits": False, "unlimited": False},
        {"balance": "0", "hasCredits": True, "unlimited": False},
    ):
        client = ScriptedAccountClient(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 10},
                    "credits": credits,
                },
            }
        )
        snapshot = asyncio.run(
            CodexAccountOracle(
                "subscription",
                client_factory=lambda client=client: client,
            ).snapshot()
        )

        assert snapshot.credits_exhausted is True
        assert client.closed


def test_explicit_single_window_plan_preserves_reserve_and_round_trips():
    from datetime import timedelta

    from agentd.coding.controller import coding_provider_policies
    from agentd.domain.models import utc_now
    from agentd.runtime.accounts import unattended_provider_wait_reason
    from agentd.runtime.governor import provider_stop_command

    now = utc_now()
    account, stop = coding_provider_policies({"provider_reserve_percent": 10})
    for used in (7, 89.99, 90, 100):
        client = ScriptedAccountClient(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "planType": "prolite",
                        "primary": {
                            "usedPercent": used,
                            "windowDurationMins": 10080,
                            "resetsAt": int((now + timedelta(days=6)).timestamp()),
                        },
                        "secondary": None,
                    }
                },
            }
        )
        snapshot = asyncio.run(
            CodexAccountOracle(
                "codex", client_factory=lambda client=client: client
            ).snapshot()
        )
        snapshot = ProviderQuotaSnapshot.from_dict(snapshot.to_dict())
        assert snapshot.metadata["reported_windows"] == ["primary"]
        reason = unattended_provider_wait_reason(snapshot, policy=account)
        command = provider_stop_command(
            snapshot,
            run_id="run",
            at=snapshot.observed_at,
            policy=stop,
            account_policy=account,
        )
        if used < 90:
            assert reason is None
            assert command is None
        else:
            assert reason == "quota_provider_pressure"
            assert command.action == "interrupt"


def test_missing_or_incomplete_window_is_not_a_single_window_plan():
    from datetime import timedelta

    from agentd.coding.controller import coding_provider_policies
    from agentd.domain.models import utc_now
    from agentd.runtime.accounts import unattended_provider_wait_reason

    now = utc_now()
    account, _ = coding_provider_policies({"provider_reserve_percent": 10})
    primary = {
        "usedPercent": 7,
        "windowDurationMins": 10080,
        "resetsAt": int((now + timedelta(days=6)).timestamp()),
    }
    for bucket in (
        {"primary": primary},
        {"primary": primary, "secondary": {}},
        {"primary": primary, "secondary": {"windowDurationMins": 300}},
        {"primary": {"usedPercent": 7}, "secondary": None},
        {"primary": None, "secondary": None},
    ):
        client = ScriptedAccountClient({"rateLimits": {"limitId": "codex", **bucket}})
        snapshot = asyncio.run(
            CodexAccountOracle(
                "codex", client_factory=lambda client=client: client
            ).snapshot()
        )
        assert (
            unattended_provider_wait_reason(snapshot, policy=account) == "quota_unknown"
        )


def test_five_hour_window_is_enforced_when_provider_adds_it():
    from datetime import timedelta

    from agentd.coding.controller import coding_provider_policies
    from agentd.domain.models import utc_now
    from agentd.runtime.accounts import unattended_provider_wait_reason
    from agentd.runtime.governor import provider_stop_command

    now = utc_now()
    weekly = {
        "usedPercent": 7,
        "windowDurationMins": 10080,
        "resetsAt": int((now + timedelta(days=6)).timestamp()),
    }
    client = ScriptedAccountClient(
        {"rateLimits": {"limitId": "codex", "primary": weekly, "secondary": None}}
    )
    oracle = CodexAccountOracle("codex", client_factory=lambda: client)
    account, stop = coding_provider_policies({"provider_reserve_percent": 10})
    first = asyncio.run(oracle.snapshot())
    assert unattended_provider_wait_reason(first, policy=account) is None
    client.response = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 90,
                "windowDurationMins": 300,
                "resetsAt": int((now + timedelta(hours=2)).timestamp()),
            },
            "secondary": weekly,
        }
    }
    changed = asyncio.run(oracle.snapshot())
    assert (
        unattended_provider_wait_reason(changed, policy=account)
        == "quota_provider_pressure"
    )
    command = provider_stop_command(
        changed,
        run_id="run",
        at=changed.observed_at,
        policy=stop,
        account_policy=account,
    )
    assert command.action == "interrupt"
