"""Check the rendered three-service coding deployment before activation."""

import json
import sys

TARGETS = {
    "coding-controller": {
        "/home/bened/.local/state/agentd/controller": False,
        "/etc/agentd/controller.json": True,
        "/home/bened/.config/gh": True,
        "/home/bened/.local/state/agentd/publication-cache": False,
        "/home/bened/.local/state/agentd/transport": True,
        "/home/bened/.local/share/agentd/codex-home": False,
    },
    "coding-worker": {
        "/home/bened/.local/state/agentd": False,
        "/home/bened/.local/state/agentd/transport": True,
        "/home/bened/.local/share/agentd/workspaces": False,
        "/home/bened/.local/share/agentd/codex-home": False,
        "/home/bened/.local/share/agentd/codex-home/config.toml": True,
        "/home/bened/.local/state/agentd/coding-profiles.json": True,
    },
    "coding-publisher": {
        "/home/bened/.local/state/agentd/controller": False,
        "/etc/agentd/controller.json": True,
        "/home/bened/.config/gh": True,
        "/home/bened/.local/state/agentd/publication-cache": False,
    },
}


def validate(config: dict) -> None:
    assert config["name"] == "agentd-selfhost-coding", "Unexpected deployment identity"
    assert set(config["services"]) == set(TARGETS), "Unexpected service"
    for name, expected in TARGETS.items():
        service = config["services"][name]
        assert service["user"] == "1000:1000" and service["read_only"]
        assert not service.get("privileged") and not service.get("ports")
        assert service.get("network_mode") not in {"host", "container"}
        assert service.get("pid") != "host" and service.get("ipc") != "host"
        assert service["cap_drop"] == ["ALL"] and not service.get("cap_add")
        options = set(service["security_opt"])
        assert "no-new-privileges:true" in options
        assert "apparmor=lxc-usernsexec" in options
        assert any(s.startswith("seccomp=") for s in options)
        assert not any(
            "TOKEN" in key or "SECRET" in key for key in service["environment"]
        )
        actual = {}
        for volume in service["volumes"]:
            assert volume["type"] == "bind" and not volume["bind"].get(
                "create_host_path"
            )
            source = volume["source"]
            assert source.startswith(
                (
                    "/home/bened/.local/state/agentd-selfhost/",
                    "/home/bened/.local/share/agentd-selfhost/",
                )
            )
            assert volume["target"] not in actual, "Duplicate mount target"
            actual[volume["target"]] = volume.get("read_only", False)
        assert actual == expected, "Unexpected mount or mount authority"
        assert service["healthcheck"].get("test"), "Missing liveness check"


if __name__ == "__main__":
    validate(json.load(sys.stdin))
    print("Coding deployment security contract passed.")
