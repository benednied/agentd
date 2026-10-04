import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from openai_codex.async_client import AsyncCodexClient

from agentd.harness.app_server import OpenAICodexClient


@dataclass(slots=True)
class RecordingSdkClient:
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def thread_start(self, params: dict[str, Any]) -> object:
        self.calls.append(("thread/start", params))
        return SimpleNamespace(thread=SimpleNamespace(id="thread-1"))

    async def thread_resume(
        self,
        thread_id: str,
        params: dict[str, Any],
    ) -> object:
        self.calls.append(("thread/resume", (thread_id, params)))
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    async def thread_read(self, thread_id: str, include_turns: bool = False) -> object:
        self.calls.append(("thread/read", (thread_id, include_turns)))
        return SimpleNamespace(thread={"id": thread_id, "turns": []})

    async def turn_start(
        self,
        thread_id: str,
        prompt: str,
        params: dict[str, Any],
    ) -> object:
        self.calls.append(("turn/start", (thread_id, prompt, params)))
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))


def test_openai_client_uses_experimental_permission_profile_wire_fields() -> None:
    sdk = RecordingSdkClient()
    client = OpenAICodexClient(cwd="/leases/job-1")
    assert client._client._sync.config.experimental_api is True
    assert client._client._sync.config.cwd == "/leases/job-1"
    client._client = cast(AsyncCodexClient, sdk)
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    async def scenario() -> str:
        thread_id = await client.start_thread(
            cwd="/leases/job-1",
            model="gpt-6-luna",
        )
        await client.resume_thread(
            thread_id,
            cwd="/leases/job-1",
            model="gpt-6-luna",
        )
        return await client.start_turn(
            thread_id,
            "do the work",
            cwd="/leases/job-1",
            model="gpt-6-luna",
            effort="xhigh",
            output_schema=schema,
        )

    turn_id = asyncio.run(scenario())

    assert turn_id == "turn-1"
    assert sdk.calls == [
        (
            "thread/start",
            {
                "approvalPolicy": "never",
                "cwd": "/leases/job-1",
                "ephemeral": False,
                "model": "gpt-6-luna",
                "permissions": "agentd-workspace",
                "runtimeWorkspaceRoots": ["/leases/job-1"],
                "serviceName": "agentd",
            },
        ),
        (
            "thread/resume",
            (
                "thread-1",
                {
                    "approvalPolicy": "never",
                    "cwd": "/leases/job-1",
                    "model": "gpt-6-luna",
                    "permissions": "agentd-workspace",
                    "runtimeWorkspaceRoots": ["/leases/job-1"],
                },
            ),
        ),
        (
            "turn/start",
            (
                "thread-1",
                "do the work",
                {
                    "approvalPolicy": "never",
                    "cwd": "/leases/job-1",
                    "effort": "xhigh",
                    "model": "gpt-6-luna",
                    "outputSchema": schema,
                    "permissions": "agentd-workspace",
                    "runtimeWorkspaceRoots": ["/leases/job-1"],
                },
            ),
        ),
    ]
    serialized = repr(sdk.calls)
    assert "sandboxPolicy" not in serialized
    assert "readOnlyAccess" not in serialized


def test_isolated_environment_uses_explicit_clean_launch(monkeypatch):
    import agentd.harness.app_server as module

    captured = []
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-inherit")
    monkeypatch.setattr(
        module, "AsyncCodexClient", lambda config: captured.append(config)
    )
    module.OpenAICodexClient(
        environment={"PATH": "/usr/bin", "CODEX_HOME": "/private/auth"},
        codex_bin="/pinned/codex",
        isolated_environment=True,
    )
    args = captured[0].launch_args_override
    assert args == (
        "/usr/bin/env",
        "-i",
        "CODEX_HOME=/private/auth",
        "PATH=/usr/bin",
        "/pinned/codex",
        "app-server",
        "--listen",
        "stdio://",
    )
    assert not any("GITHUB_TOKEN" in value for value in args)


def test_read_thread_only_requests_saved_history() -> None:
    sdk = RecordingSdkClient()
    client = OpenAICodexClient()
    client._client = cast(AsyncCodexClient, sdk)
    assert asyncio.run(client.read_thread("saved-thread")) == {
        "id": "saved-thread",
        "turns": [],
    }
    assert sdk.calls == [("thread/read", ("saved-thread", True))]
