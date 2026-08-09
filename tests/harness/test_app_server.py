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
            model="gpt-5.6-terra",
        )
        await client.resume_thread(
            thread_id,
            cwd="/leases/job-1",
            model="gpt-5.6-terra",
        )
        return await client.start_turn(
            thread_id,
            "do the work",
            cwd="/leases/job-1",
            model="gpt-5.6-terra",
            effort="medium",
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
                "model": "gpt-5.6-terra",
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
                    "model": "gpt-5.6-terra",
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
                    "effort": "medium",
                    "model": "gpt-5.6-terra",
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
