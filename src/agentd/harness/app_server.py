"""Typed boundary around the official Codex Python SDK/App Server client.

The rest of agentd consumes a deliberately small, SDK-neutral protocol.  This
keeps generated App Server models out of the scheduler and makes driver tests
fully scriptable without starting Codex or making network calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast, runtime_checkable

from openai_codex import __version__ as installed_sdk_version
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexConfig
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from agentd.domain.models import JsonValue

PINNED_OPENAI_CODEX_VERSION = "0.144.4"
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
DEFAULT_REASONING_EFFORT = "medium"
TERMINAL_EVENT_METHODS = frozenset({"turn/completed", "turn/failed", "error"})


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
        writable_roots: Sequence[str],
        readable_roots: Sequence[str],
    ) -> str: ...

    def events(self, turn_id: str) -> AsyncIterator[AppServerEvent]: ...

    async def steer(self, thread_id: str, turn_id: str, instruction: str) -> None: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> None: ...

    async def account_rate_limits(self) -> dict[str, JsonValue]: ...


class OpenAICodexClient:
    """Adapter for ``openai-codex==0.144.4`` and its pinned App Server."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        codex_bin: str | None = None,
    ) -> None:
        if installed_sdk_version != PINNED_OPENAI_CODEX_VERSION:
            raise RuntimeError(
                "Unsupported openai-codex SDK version "
                f"{installed_sdk_version!r}; expected {PINNED_OPENAI_CODEX_VERSION!r}"
            )
        self._client = AsyncCodexClient(
            CodexConfig(
                codex_bin=codex_bin,
                env=dict(environment or {}),
                client_name="agentd",
                client_title="agentd control plane",
                experimental_api=False,
            )
        )
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
        except BaseException:
            await self._client.close()
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
                "sandbox": "workspace-write",
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
                "sandbox": "workspace-write",
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
        writable_roots: Sequence[str],
        readable_roots: Sequence[str],
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
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": list(writable_roots),
                    "networkAccess": False,
                    "excludeSlashTmp": True,
                    "excludeTmpdirEnvVar": True,
                    "readOnlyAccess": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": list(readable_roots),
                    },
                },
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
        response = await self._client.request(
            "account/rateLimits/read",
            None,
            response_model=GetAccountRateLimitsResponse,
        )
        return cast(
            dict[str, JsonValue],
            response.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )


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
