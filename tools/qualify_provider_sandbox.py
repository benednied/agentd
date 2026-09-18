#!/usr/bin/env python3
"""Bounded provider smoke, run inside an operator-preflighted worker runtime.

This is deliberately not a claim that the GitHub-to-PR vertical slice passed.
Never run this against an unverified permission profile or production repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from contextlib import suppress
from math import isfinite
from pathlib import Path
from typing import Any

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import CommandExecResponse, GetAccountResponse

from agentd.harness.app_server import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_PERMISSION_PROFILE,
    OpenAICodexClient,
)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("malformed provider object; qualification must wait")
    return value


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(workspace), "-c", "core.hooksPath=/dev/null", *arguments],
        text=True,
    ).strip()


async def qualify(args: argparse.Namespace) -> dict[str, Any]:
    # The deployed probe checks the pinned runtime, actual permission profile,
    # credential/state denial, restricted writes and network isolation.
    subprocess.run([sys.executable, str(args.preflight)], check=True, timeout=60)
    root = args.workspace_root.resolve(strict=True)
    workspace = Path(tempfile.mkdtemp(prefix=".issue60-smoke.", dir=root))
    git(workspace, "init", "-q")
    git(
        workspace,
        "-c",
        "user.name=agentd qualification",
        "-c",
        "user.email=agentd@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "qualification base",
    )
    base = git(workspace, "rev-parse", "HEAD")
    client = OpenAICodexClient(cwd=str(workspace))
    thread = turn = None
    maximum_observed_tokens = 0
    terminal = None
    try:
        await client.start()
        # Normal managed credential refresh; never copy, print or export tokens.
        await client._client.request(
            "account/read", {"refreshToken": True}, response_model=GetAccountResponse
        )
        raw = await client.account_rate_limits()
        buckets = _object(raw.get("rateLimitsByLimitId") or {})
        bucket = _object(buckets.get("codex") or raw.get("rateLimits") or {})
        percentages: list[float] = []
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if window is None:
                continue
            used = _object(window).get("usedPercent")
            if (
                isinstance(used, bool)
                or not isinstance(used, int | float)
                or not isfinite(used)
                or not 0 <= used <= 100
            ):
                raise RuntimeError(
                    "malformed provider percentage; qualification must wait"
                )
            percentages.append(float(used))
        if (
            not percentages
            or max(percentages) >= 75
            or bucket.get("rateLimitReachedType")
        ):
            raise RuntimeError("provider quota is unknown or insufficient; wait")
        used_percent = max(percentages)
        thread = await client.start_thread(cwd=str(workspace), model=args.model)
        turn = await client.start_turn(
            thread,
            "Create hello.py containing exactly a function add(a, b) that returns "
            "a + b. Do not run git commands. Stop after creating that one file.",
            cwd=str(workspace),
            model=args.model,
            effort="low",
            output_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        )
        async with asyncio.timeout(args.timeout):
            async for event in client.events(turn):
                if event.method == "thread/tokenUsage/updated":
                    usage = _object(event.payload.get("tokenUsage"))
                    tokens = _object(usage.get("total")).get("totalTokens")
                    if (
                        isinstance(tokens, bool)
                        or not isinstance(tokens, int)
                        or tokens < 0
                    ):
                        raise RuntimeError("malformed trusted token counter")
                    maximum_observed_tokens = max(maximum_observed_tokens, tokens)
                    if maximum_observed_tokens >= args.maximum_tokens:
                        raise RuntimeError("smoke token ceiling reached")
                if event.method in {"turn/completed", "turn/failed"}:
                    terminal = _object(event.payload.get("turn")).get("status")
        if terminal != "completed":
            raise RuntimeError("provider turn did not complete")
    finally:
        if thread and turn:
            with suppress(Exception):
                await client.interrupt(thread, turn)
        await client.close()

    # Never import or execute model-produced code in the credentialed controller.
    with CodexClient(
        CodexConfig(cwd=str(workspace), experimental_api=True)
    ) as validator:
        validator.initialize()
        result = validator.request(
            "command/exec",
            {
                "command": [
                    sys.executable,
                    "-c",
                    "from hello import add; "
                    "assert add(2, 3) == 5; assert add(-2, 2) == 0",
                ],
                "cwd": str(workspace),
                "timeoutMs": 10000,
                "permissionProfile": DEFAULT_PERMISSION_PROFILE,
            },
            response_model=CommandExecResponse,
        )
    if result.exit_code != 0:
        raise RuntimeError("sandbox validation failed")
    git(workspace, "add", "hello.py")
    git(
        workspace,
        "-c",
        "user.name=agentd qualification",
        "-c",
        "user.email=agentd@example.invalid",
        "commit",
        "-qm",
        "qualification generated addition",
    )
    return {
        "scope": "provider-and-sandbox-feasibility-only",
        "model": args.model,
        "permission_profile": DEFAULT_PERMISSION_PROFILE,
        "provider_used_percent_before": used_percent,
        "base_commit": base,
        "result_commit": git(workspace, "rev-parse", "HEAD"),
        "thread_id": thread,
        "turn_id": turn,
        "workspace": str(workspace),
        "observed_tokens": maximum_observed_tokens,
        "sandbox_validation_exit": result.exit_code,
        "passed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_CODEX_MODEL)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--maximum-tokens", type=int, default=60000)
    args = parser.parse_args()
    if args.timeout <= 0 or args.maximum_tokens <= 0:
        parser.error("timeout and maximum-tokens must be positive")
    print(json.dumps(asyncio.run(qualify(args)), sort_keys=True))


if __name__ == "__main__":
    main()
