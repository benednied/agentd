"""Read-only ChatGPT quota observations from Codex App Server."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from agentd.domain.models import JsonValue, ProviderQuotaSnapshot
from agentd.harness.app_server import AppServerClient, OpenAICodexClient


@runtime_checkable
class ProviderQuotaStore(Protocol):
    """Persistence surface used by :class:`CodexAccountOracle`."""

    def append_provider_quota_snapshot(
        self, snapshot: ProviderQuotaSnapshot
    ) -> None: ...


@runtime_checkable
class AccountOracle(Protocol):
    """Fetch one provider-owned quota observation without applying policy."""

    async def snapshot(self) -> ProviderQuotaSnapshot: ...


class CodexAccountOracle:
    """Translate App Server rate-limit buckets into durable provider snapshots."""

    def __init__(
        self,
        pool_id: str,
        *,
        bucket_id: str = "codex",
        client_factory: Callable[[], AppServerClient] = OpenAICodexClient,
        store: ProviderQuotaStore | None = None,
    ) -> None:
        if not pool_id.strip():
            raise ValueError("A Codex quota oracle requires a pool identifier")
        if not bucket_id.strip():
            raise ValueError("A Codex quota oracle requires a bucket identifier")
        self._pool_id = pool_id
        self._bucket_id = bucket_id
        self._client_factory = client_factory
        self._store = store

    async def snapshot(self) -> ProviderQuotaSnapshot:
        client = self._client_factory()
        await client.start()
        try:
            payload = await client.account_rate_limits()
            snapshot = self._parse(payload, client)
        finally:
            await client.close()
        if self._store is not None:
            self._store.append_provider_quota_snapshot(snapshot)
        return snapshot

    def _parse(
        self,
        payload: Mapping[str, JsonValue],
        client: AppServerClient,
    ) -> ProviderQuotaSnapshot:
        bucket = _select_bucket(payload, self._bucket_id)
        primary = _mapping(bucket.get("primary"))
        secondary = _mapping(bucket.get("secondary"))
        reached_type = _optional_string(bucket.get("rateLimitReachedType"))
        credits = bucket.get("credits")
        primary_used = _optional_percent(primary.get("usedPercent"))
        secondary_used = _optional_percent(secondary.get("usedPercent"))
        metadata = client.metadata
        return ProviderQuotaSnapshot(
            pool_id=self._pool_id,
            bucket_id=self._bucket_id,
            primary_used_percent=primary_used,
            primary_window_minutes=_optional_positive_int(
                primary.get("windowDurationMins")
            ),
            primary_reset_at=_optional_timestamp(primary.get("resetsAt")),
            secondary_used_percent=secondary_used,
            secondary_window_minutes=_optional_positive_int(
                secondary.get("windowDurationMins")
            ),
            secondary_reset_at=_optional_timestamp(secondary.get("resetsAt")),
            reached=(
                reached_type is not None or primary_used == 100 or secondary_used == 100
            ),
            credits_exhausted=_credits_exhausted(credits),
            rate_limit_reached_type=reached_type,
            plan_type=_optional_string(bucket.get("planType")),
            credits=credits,
            rate_limit_reset_credits=payload.get("rateLimitResetCredits"),
            metadata={
                "sdk_version": metadata.sdk_version,
                "runtime_version": metadata.runtime_version,
                "reported_bucket_id": _optional_string(bucket.get("limitId")),
            },
        )


def _select_bucket(
    payload: Mapping[str, JsonValue], bucket_id: str
) -> Mapping[str, JsonValue]:
    buckets = _mapping(payload.get("rateLimitsByLimitId"))
    selected = _mapping(buckets.get(bucket_id))
    if selected:
        return selected
    legacy = _mapping(payload.get("rateLimits"))
    if legacy and _optional_string(legacy.get("limitId")) in {None, bucket_id}:
        return legacy
    available = ", ".join(sorted(str(key) for key in buckets)) or "(none)"
    raise ValueError(
        f"Codex rate-limit bucket {bucket_id!r} was not reported; "
        f"available: {available}"
    )


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    return value


def _optional_string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_percent(value: JsonValue) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Codex usedPercent must be numeric")
    percent = float(value)
    if not 0 <= percent <= 100:
        raise ValueError("Codex usedPercent must be between 0 and 100")
    return percent


def _optional_positive_int(value: JsonValue) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Codex windowDurationMins must be an integer")
    if value <= 0:
        raise ValueError("Codex windowDurationMins must be positive")
    return value


def _optional_timestamp(value: JsonValue) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Codex resetsAt must be a Unix timestamp")
    return datetime.fromtimestamp(value, tz=UTC)


def _credits_exhausted(value: JsonValue) -> bool | None:
    credits = _mapping(value)
    if not credits:
        return None
    has_credits = credits.get("hasCredits")
    unlimited = credits.get("unlimited")
    if has_credits is False:
        return True
    if unlimited is True:
        return False
    balance = credits.get("balance")
    if isinstance(balance, bool):
        return None
    if isinstance(balance, int | float | str):
        try:
            parsed = Decimal(str(balance))
        except InvalidOperation:
            return False if has_credits is True else None
        if parsed.is_finite():
            return parsed <= 0
    return False if has_credits is True else None
