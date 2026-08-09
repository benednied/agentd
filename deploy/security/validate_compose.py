#!/usr/bin/env python3
"""Validate the fully rendered Compose security contract without Docker API access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED_MOUNTS = {
    "/home/bened/.local/state/agentd": "/home/bened/.local/state/agentd",
    "/home/bened/.local/share/agentd/workspaces": (
        "/home/bened/.local/share/agentd/workspaces"
    ),
    "/home/bened/.local/share/agentd/codex-home": (
        "/home/bened/.local/share/agentd/codex-home"
    ),
    "/home/bened/.cache/uv": "/home/bened/.cache/uv",
    "/home/bened/goldenage": "/home/bened/goldenage",
}
EXPECTED_ENVIRONMENT = {
    "HOME": "/home/bened",
    "AGENTD_DB": "/home/bened/.local/state/agentd/state.sqlite",
    "AGENTD_WORKSPACE_ROOT": "/home/bened/.local/share/agentd/workspaces",
    "AGENTD_CODEX_HOME": "/home/bened/.local/share/agentd/codex-home",
    "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
    "AGENTD_CODEX_REASONING_EFFORT": "medium",
    "CODEX_HOME": "/home/bened/.local/share/agentd/codex-home",
    "UV_CACHE_DIR": "/home/bened/.cache/uv",
    "XDG_CACHE_HOME": "/tmp/agentd-cache",
    "TMPDIR": "/tmp",
    "GIT_AUTHOR_NAME": "agentd automation",
    "GIT_AUTHOR_EMAIL": "agentd@localhost",
    "GIT_COMMITTER_NAME": "agentd automation",
    "GIT_COMMITTER_EMAIL": "agentd@localhost",
}
REQUIRED_CONFIG_ENVIRONMENT = {
    "AGENTD_POLL_SECONDS",
    "AGENTD_ACCOUNT_POLL_SECONDS",
    "AGENTD_ACCOUNT_STALE_SECONDS",
    "AGENTD_QUOTA_TOP_UP_TOKENS",
    "AGENTD_HARD_CAP_GRACE_SECONDS",
}


class SecurityValidationError(ValueError):
    """Rendered deployment violates the reviewed container contract."""


def _environment_map(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            key, separator, value = str(item).partition("=")
            if not separator:
                raise SecurityValidationError("environment entries must set values")
            result[key] = value
        return result
    raise SecurityValidationError("service environment must be a map or list")


def _memory_is_12_gib(value: object) -> bool:
    if isinstance(value, int | float):
        return int(value) == 12 * 1024**3
    normalized = str(value).strip().lower()
    return normalized in {"12g", "12gb", "12gib", "12884901888"}


def validate_service(service: dict[str, Any]) -> None:
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    image = str(service.get("image", ""))
    require(
        bool(re.fullmatch(r"[^\s@:]+(?:/[^\s@:]+)*:[0-9a-f]{40}", image))
        or bool(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image)),
        "image must use a 40-character Git SHA tag or immutable digest",
    )
    require(service.get("user") == "1000:1000", "container user must be 1000:1000")
    require(service.get("read_only") is True, "root filesystem must be read-only")
    require(service.get("privileged", False) is False, "privileged mode must be false")
    require(
        service.get("pull_policy") == "never", "runtime image pulls must be disabled"
    )
    require(service.get("working_dir") == "/home/bened/goldenage", "unexpected workdir")
    require(service.get("init") is True, "container init must be enabled")
    require(service.get("cap_drop") == ["ALL"], "all Linux capabilities must drop")
    require(not service.get("cap_add"), "capabilities must not be added")
    require(service.get("pids_limit") == 512, "PID limit must be 512")
    require(float(service.get("cpus", 0)) == 6.0, "CPU limit must be 6")
    require(_memory_is_12_gib(service.get("mem_limit")), "memory limit must be 12 GiB")
    for unsafe_key in (
        "devices",
        "device_cgroup_rules",
        "ports",
        "expose",
        "pid",
        "ipc",
        "userns_mode",
    ):
        require(not service.get(unsafe_key), f"{unsafe_key} must not be configured")
    require(service.get("network_mode") != "host", "host networking is forbidden")

    security_options = {str(item) for item in service.get("security_opt", [])}
    require(
        "no-new-privileges:true" in security_options,
        "no-new-privileges must be enabled",
    )
    require(
        any(option.endswith("seccomp-agentd.json") for option in security_options),
        "reviewed seccomp profile must be selected",
    )

    environment = _environment_map(service.get("environment", {}))
    for name, expected in EXPECTED_ENVIRONMENT.items():
        require(environment.get(name) == expected, f"unexpected {name}")
    for name in REQUIRED_CONFIG_ENVIRONMENT:
        require(bool(environment.get(name)), f"missing {name}")
    require(
        set(environment) == set(EXPECTED_ENVIRONMENT) | REQUIRED_CONFIG_ENVIRONMENT,
        "service environment contains an unreviewed variable",
    )

    mounts: dict[str, str] = {}
    for mount in service.get("volumes", []):
        if not isinstance(mount, dict):
            errors.append("all mounts must use long syntax")
            continue
        source = str(mount.get("source", ""))
        target = str(mount.get("target", ""))
        mounts[target] = source
        require(mount.get("type") == "bind", f"{target} must be a bind mount")
        require(
            mount.get("read_only", False) is False, f"{target} must declare write mode"
        )
        bind = mount.get("bind", {})
        require(
            isinstance(bind, dict) and bind.get("create_host_path") is False,
            f"{target} must not be created implicitly",
        )
        require("docker.sock" not in source, "Docker socket must never be mounted")
        require(
            source not in {"/", "/home", "/home/bened", "/root"},
            "broad host mount",
        )
    require(
        mounts == EXPECTED_MOUNTS,
        "bind mounts must match the exact reviewed paths",
    )

    tmpfs_entries = [str(item) for item in service.get("tmpfs", [])]
    tmpfs = "\n".join(tmpfs_entries)
    require(len(tmpfs_entries) == 2, "only the two reviewed tmpfs mounts are allowed")
    require("/tmp:" in tmpfs and "/run/agentd:" in tmpfs, "required tmpfs missing")
    require("noexec" in tmpfs and "nosuid" in tmpfs and "nodev" in tmpfs, "tmpfs flags")
    require("uid=1000" in tmpfs and "gid=1000" in tmpfs, "tmpfs owner must be 1000")

    if errors:
        raise SecurityValidationError("; ".join(errors))


def validate_compose(document: dict[str, Any]) -> None:
    services = document.get("services")
    if not isinstance(services, dict) or set(services) != {"agentd"}:
        raise SecurityValidationError("Compose must define only the agentd service")
    service = services["agentd"]
    if not isinstance(service, dict):
        raise SecurityValidationError("agentd service must be an object")
    validate_service(service)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: validate_compose.py RENDERED_COMPOSE_JSON", file=sys.stderr)
        return 2
    payload = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SecurityValidationError("rendered Compose document must be an object")
    validate_compose(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
