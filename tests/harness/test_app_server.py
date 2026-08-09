import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from openai_codex.async_client import AsyncCodexClient

from agentd.harness.app_server import OpenAICodexClient


@dataclass(slots=True)
class RecordingSdkClient:
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    async def turn_start(
        self,
        thread_id: str,
        prompt: str,
        params: dict[str, Any],
    ) -> object:
        self.calls.append((thread_id, prompt, params))
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))


def test_openai_client_applies_restricted_workspace_write_policy() -> None:
    sdk = RecordingSdkClient()
    client = OpenAICodexClient()
    client._client = cast(AsyncCodexClient, sdk)
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    turn_id = asyncio.run(
        client.start_turn(
            "thread-1",
            "do the work",
            cwd="/leases/job-1",
            model="gpt-5.6-terra",
            effort="medium",
            output_schema=schema,
            writable_roots=("/leases/job-1",),
            readable_roots=("/leases/job-1", "/opt/agentd/toolchain"),
        )
    )

    assert turn_id == "turn-1"
    assert sdk.calls == [
        (
            "thread-1",
            "do the work",
            {
                "approvalPolicy": "never",
                "cwd": "/leases/job-1",
                "effort": "medium",
                "model": "gpt-5.6-terra",
                "outputSchema": schema,
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": ["/leases/job-1"],
                    "networkAccess": False,
                    "excludeSlashTmp": True,
                    "excludeTmpdirEnvVar": True,
                    "readOnlyAccess": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": [
                            "/leases/job-1",
                            "/opt/agentd/toolchain",
                        ],
                    },
                },
            },
        )
    ]
