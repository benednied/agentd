import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
            sdk_version="0.144.4",
            runtime_version="0.144.4",
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
        writable_roots: Sequence[str],
        readable_roots: Sequence[str],
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
        "sdk_version": "0.144.4",
        "runtime_version": "0.144.4",
        "reported_bucket_id": "codex",
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
