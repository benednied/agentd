from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
UV_CACHE = "/home/bened/.cache/uv"


def test_uv_toolchain_is_service_writable_but_model_read_only() -> None:
    compose = json.loads((REPOSITORY / "deploy" / "compose.yaml").read_text())
    service = compose["services"]["agentd"]
    cache_mount = next(
        mount for mount in service["volumes"] if mount["target"] == UV_CACHE
    )
    assert cache_mount == {
        "type": "bind",
        "source": "${AGENTD_UV_CACHE:?set exact dedicated uv cache}",
        "target": UV_CACHE,
        "read_only": False,
        "bind": {"create_host_path": False},
    }

    config = tomllib.loads(
        (REPOSITORY / "deploy" / "container" / "config.toml").read_text()
    )
    filesystem = config["permissions"]["agentd-workspace"]["filesystem"]
    assert filesystem[UV_CACHE] == "read"
    assert filesystem[":workspace_roots"] == {".": "write"}
    assert [value for value in filesystem.values() if value == "write"] == []
    assert filesystem["/home/bened/.local/share/agentd/workspaces"] == "deny"
