"""Typed boundary around the official Codex Python SDK/App Server client.

The rest of agentd consumes a deliberately small, SDK-neutral protocol.  This
keeps generated App Server models out of the scheduler and makes driver tests
fully scriptable without starting Codex or making network calls.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from openai_codex import __version__ as installed_sdk_version
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexConfig
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from agentd.codex_versions import PINNED_OPENAI_CODEX_VERSION
from agentd.domain.models import JsonValue
from agentd.harness.codex_auth import CodexAccess, CodexRefreshUnavailable, CodexSession

DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_PERMISSION_PROFILE = "agentd-workspace"
EXTERNAL_AUTH_CONFIG = 'cli_auth_credentials_store="ephemeral"'
_QUOTA_UNAUTHORIZED = re.compile(
    r"failed to fetch codex rate limits: GET \S+ failed: 401(?: Unauthorized)?;"
)
# Error notifications describe a turn-level problem, but they are not a final
# turn result.  In particular, ``ErrorNotification.willRetry`` may be true and
# the App Server can continue the same turn after emitting one.  Match the
# pinned SDK's turn collector and keep the stream open until a definitive turn
# notification arrives.
TERMINAL_EVENT_METHODS = frozenset({"turn/completed", "turn/failed"})


@dataclass(frozen=True, slots=True)
class AppServerMetadata:
    """Version and platform identity reported by one App Server connection."""

    sdk_version: str
    runtime_version: str
    server_name: str | None = None
    user_agent: str | None = None
    platform_family: str | None = None
    platform_os: str | None = None


@dataclass(frozen=True, slots=True)
class AppServerEvent:
    """One normalized App Server notification."""

    method: str
    payload: dict[str, JsonValue]


@runtime_checkable
class AppServerClient(Protocol):
    """Minimal App Server surface required by the Codex control plane."""

    @property
    def metadata(self) -> AppServerMetadata: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str,
    ) -> str: ...

    async def resume_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        model: str,
    ) -> str: ...

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        model: str,
        effort: str,
        output_schema: Mapping[str, JsonValue],
    ) -> str: ...

    def events(self, turn_id: str) -> AsyncIterator[AppServerEvent]: ...

    async def steer(self, thread_id: str, turn_id: str, instruction: str) -> None: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> None: ...

    async def account_rate_limits(self) -> dict[str, JsonValue]: ...

    async def read_thread(self, thread_id: str) -> dict[str, JsonValue]: ...


class OpenAICodexClient:
    """Adapter for the reviewed ``openai-codex`` pin and its bundled App Server."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        codex_bin: str | None = None,
        cwd: str | None = None,
        isolated_environment: bool = False,
        auth_home: Path | None = None,
    ) -> None:
        if installed_sdk_version != PINNED_OPENAI_CODEX_VERSION:
            raise RuntimeError(
                "Unsupported openai-codex SDK version "
                f"{installed_sdk_version!r}; expected {PINNED_OPENAI_CODEX_VERSION!r}"
            )
        self._session = CodexSession(auth_home) if auth_home is not None else None
        self._access: CodexAccess | None = None
        overrides = (EXTERNAL_AUTH_CONFIG,) if self._session is not None else ()
        launch_args: tuple[str, ...] | None = None
        if isolated_environment:
            from codex_cli_bin import bundled_codex_path

            explicit_environment = dict(environment or {})
            if any(
                "=" in key or "\0" in key or "\0" in value
                for key, value in explicit_environment.items()
            ):
                raise ValueError("isolated SDK environment is invalid")
            # The SDK normally overlays env on os.environ. Start through env
            # -i so model subprocesses cannot inherit controller/worker secrets.
            launch_args = (
                "/usr/bin/env",
                "-i",
                *(
                    f"{key}={value}"
                    for key, value in sorted(explicit_environment.items())
                ),
                str(codex_bin or bundled_codex_path()),
                *(arg for value in overrides for arg in ("--config", value)),
                "app-server",
                "--listen",
                "stdio://",
            )
        self._client = AsyncCodexClient(
            CodexConfig(
                codex_bin=codex_bin,
                cwd=cwd,
                env=dict(environment or {}),
                client_name="agentd",
                client_title="agentd control plane",
                experimental_api=True,
                config_overrides=overrides,
                launch_args_override=launch_args,
            )
        )
        if self._session is not None:
            # The pinned async SDK wraps the sync transport but does not expose
            # its server-request callback. Handle refresh on the reader thread.
            self._client._sync._approval_handler = self._server_request
        self._metadata: AppServerMetadata | None = None

    @property
    def metadata(self) -> AppServerMetadata:
        if self._metadata is None:
            raise RuntimeError("Codex App Server client has not been started")
        return self._metadata

    async def start(self) -> None:
        if self._metadata is not None:
            return
        await self._client.start()
        try:
            initialized = await self._client.initialize()
            if self._session is not None:
                await self._login(await asyncio.to_thread(self._session.access))
        except BaseException:
            await self.close()
            raise
        server = initialized.serverInfo
        self._metadata = AppServerMetadata(
            sdk_version=installed_sdk_version,
            runtime_version=(
                server.version
                if server is not None and server.version
                else _bundled_runtime_version()
            ),
            server_name=server.name if server is not None else None,
            user_agent=initialized.userAgent,
            platform_family=initialized.platformFamily,
            platform_os=initialized.platformOs,
        )

    async def close(self) -> None:
        await self._client.close()
        self._metadata = None
        self._access = None

    async def _login(self, access: CodexAccess) -> None:
        try:
            await self._client.account_login_start(
                {"type": "chatgptAuthTokens", **codex_access_params(access)}
            )
        except Exception:
            raise CodexRefreshUnavailable(
                "Codex App Server rejected external auth"
            ) from None
        self._access = access

    def _server_request(
        self, method: str, params: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue]:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}
        if method != "account/chatgptAuthTokens/refresh" or self._session is None:
            return {}
        current = self._access
        if current is None or (params or {}).get("previousAccountId") not in (
            None,
            current.account_id,
        ):
            return {}
        self._access = self._session.access(rejected=current)
        return codex_access_params(self._access)

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str,
    ) -> str:
        response = await self._client.thread_start(
            {
                "approvalPolicy": "never",
                "cwd": cwd,
                "ephemeral": False,
                "model": model,
                "permissions": DEFAULT_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [cwd],
                "serviceName": "agentd",
            }
        )
        return response.thread.id

    async def resume_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        model: str,
    ) -> str:
        response = await self._client.thread_resume(
            thread_id,
            {
                "approvalPolicy": "never",
                "cwd": cwd,
                "model": model,
                "permissions": DEFAULT_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [cwd],
            },
        )
        return response.thread.id

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
        response = await self._client.turn_start(
            thread_id,
            prompt,
            {
                "approvalPolicy": "never",
                "cwd": cwd,
                "effort": effort,
                "model": model,
                "outputSchema": dict(output_schema),
                "permissions": DEFAULT_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [cwd],
            },
        )
        return response.turn.id

    async def events(self, turn_id: str) -> AsyncIterator[AppServerEvent]:
        try:
            while True:
                notification = await self._client.next_turn_notification(turn_id)
                yield AppServerEvent(
                    method=notification.method,
                    payload=_payload_to_json(notification.payload),
                )
                if notification.method in TERMINAL_EVENT_METHODS:
                    break
        finally:
            self._client.unregister_turn_notifications(turn_id)

    async def steer(self, thread_id: str, turn_id: str, instruction: str) -> None:
        await self._client.turn_steer(thread_id, turn_id, instruction)

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self._client.turn_interrupt(thread_id, turn_id)

    async def account_rate_limits(self) -> dict[str, JsonValue]:
        try:
            return await self._read_rate_limits()
        except JsonRpcError as error:
            if self._session is None:
                raise
            if self._access is None or not _QUOTA_UNAUTHORIZED.match(error.message):
                raise CodexRefreshUnavailable("Codex quota refresh failed") from None
        # The quota RPC does not initiate external refresh on HTTP 401. Retry
        # once with the shared replacement access token.
        await self._login(
            await asyncio.to_thread(self._session.access, rejected=self._access)
        )
        try:
            return await self._read_rate_limits()
        except JsonRpcError:
            raise CodexRefreshUnavailable("Codex quota refresh failed") from None

    async def _read_rate_limits(self) -> dict[str, JsonValue]:
        response = await self._client.request(
            "account/rateLimits/read",
            None,
            response_model=GetAccountRateLimitsResponse,
        )
        return cast(
            dict[str, JsonValue],
            response.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )

    async def read_thread(self, thread_id: str) -> dict[str, JsonValue]:
        """Inspect saved history without loading, resuming, or starting a turn."""
        response = await self._client.thread_read(thread_id, include_turns=True)
        return _payload_to_json(response.thread)


def codex_access_params(access: CodexAccess) -> dict[str, JsonValue]:
    return {"accessToken": access.token, "chatgptAccountId": access.account_id}


def _payload_to_json(payload: object) -> dict[str, JsonValue]:
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        raw = model_dump(by_alias=True, exclude_none=True, mode="json")
    elif is_dataclass(payload) and not isinstance(payload, type):
        raw = asdict(payload)
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        raise TypeError(
            f"Unsupported App Server notification payload: {type(payload).__name__}"
        )
    if not isinstance(raw, dict):
        raise TypeError("App Server notification payload must be an object")
    return cast(dict[str, JsonValue], raw)


def _bundled_runtime_version() -> str:
    try:
        return version("openai-codex-cli-bin")
    except PackageNotFoundError:
        # Published SDK builds pin the runtime to the same version.  Keeping the
        # fallback explicit makes metadata deterministic for package-light tests.
        return PINNED_OPENAI_CODEX_VERSION
