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
    "/home/bened/.local/share/agentd/codex-home/config.toml": (
        "/home/bened/.local/share/agentd/codex-home/config.toml"
    ),
    "/home/bened/.cache/uv": "/home/bened/.cache/uv",
    "/home/bened/goldenage": "/home/bened/goldenage",
}
READ_ONLY_MOUNTS = {
    "/home/bened/.local/share/agentd/codex-home/config.toml",
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
    "AGENTD_LOG_LEVEL",
    "AGENTD_LOG_FORMAT",
}
EXPECTED_SECURITY_OPTIONS = {
    "no-new-privileges:true",
    "apparmor=lxc-usernsexec",
    "seccomp=./container/seccomp-agentd.json",
}
EXPECTED_TMPFS = {
    "/tmp": {
        "rw",
        "noexec",
        "nosuid",
        "nodev",
        "size=1073741824",
        "uid=1000",
        "gid=1000",
        "mode=1770",
    },
    "/run/agentd": {
        "rw",
        "noexec",
        "nosuid",
        "nodev",
        "size=16777216",
        "uid=1000",
        "gid=1000",
        "mode=0700",
    },
}
EXPECTED_ENTRYPOINT_COMMAND = (
    "/opt/agentd/venv/bin/python "
    "/opt/agentd/security/runtime_sandbox_probe.py && "
    'exec /opt/agentd/venv/bin/agentd "$@"'
)
EXPECTED_ENTRYPOINTS = (
    [
        "/bin/sh",
        "-ec",
        EXPECTED_ENTRYPOINT_COMMAND,
        "agentd-entrypoint",
    ],
    [
        "/bin/sh",
        "-ec",
        EXPECTED_ENTRYPOINT_COMMAND.replace("$", "$$"),
        "agentd-entrypoint",
    ],
)


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


def validate_service(
    service: dict[str, Any],
    *,
    expected_mounts: dict[str, str] | None = None,
) -> None:
    expected_mounts = EXPECTED_MOUNTS if expected_mounts is None else expected_mounts
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
    require(
        service.get("entrypoint") in EXPECTED_ENTRYPOINTS,
        "container startup must run the runtime security preflight",
    )
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

    raw_security_options = service.get("security_opt", [])
    security_options = (
        {str(item) for item in raw_security_options}
        if isinstance(raw_security_options, list)
        else set()
    )
    require(
        "no-new-privileges:true" in security_options,
        "no-new-privileges must be enabled",
    )
    require(
        "apparmor=lxc-usernsexec" in security_options,
        "the reviewed user-namespace AppArmor profile must be selected",
    )
    require(
        "seccomp=./container/seccomp-agentd.json" in security_options,
        "reviewed seccomp profile must be selected",
    )
    require(
        security_options == EXPECTED_SECURITY_OPTIONS
        and len(raw_security_options) == len(EXPECTED_SECURITY_OPTIONS),
        "service security options must exactly match the reviewed values",
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
            bool(mount.get("read_only", False)) == (target in READ_ONLY_MOUNTS),
            f"{target} has unexpected write mode",
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
        mounts == expected_mounts,
        "bind mounts must match the exact reviewed paths",
    )

    raw_tmpfs = service.get("tmpfs", [])
    tmpfs_entries = raw_tmpfs if isinstance(raw_tmpfs, list) else []
    parsed_tmpfs: dict[str, set[str]] = {}
    for raw_entry in tmpfs_entries:
        if not isinstance(raw_entry, str):
            errors.append("all tmpfs mounts must use string syntax")
            continue
        target, separator, raw_options = raw_entry.partition(":")
        options = raw_options.split(",") if separator and raw_options else []
        if target in parsed_tmpfs:
            errors.append(f"duplicate tmpfs target: {target}")
            continue
        parsed_tmpfs[target] = set(options)
        require(
            len(options) == len(parsed_tmpfs[target]),
            f"{target} contains duplicate tmpfs options",
        )
    require(
        len(tmpfs_entries) == len(EXPECTED_TMPFS),
        "only the two reviewed tmpfs mounts are allowed",
    )
    require(
        parsed_tmpfs == EXPECTED_TMPFS,
        "each tmpfs target must use its complete reviewed option set",
    )

    if errors:
        raise SecurityValidationError("; ".join(errors))


def validate_compose(
    document: dict[str, Any],
    *,
    expected_mounts: dict[str, str] | None = None,
) -> None:
    services = document.get("services")
    if not isinstance(services, dict) or set(services) != {"agentd"}:
        raise SecurityValidationError("Compose must define only the agentd service")
    service = services["agentd"]
    if not isinstance(service, dict):
        raise SecurityValidationError("agentd service must be an object")
    validate_service(service, expected_mounts=expected_mounts)


def _mount_policy(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"mounts"}:
        raise SecurityValidationError("mount policy must contain only 'mounts'")
    mounts = raw["mounts"]
    if not isinstance(mounts, dict):
        raise SecurityValidationError("mount policy 'mounts' must be an object")
    expected = {str(target): str(source) for target, source in mounts.items()}
    if set(expected) != set(EXPECTED_MOUNTS):
        raise SecurityValidationError("mount policy must name every reviewed target")
    for target, source in expected.items():
        path = Path(source)
        if not path.is_absolute():
            raise SecurityValidationError(
                f"mount policy source for {target} is relative"
            )
        if source in {"/", "/home", "/home/bened", "/root"}:
            raise SecurityValidationError(f"mount policy source for {target} is broad")
        if "docker.sock" in source:
            raise SecurityValidationError("Docker socket must never be mounted")
    return expected


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) not in {1, 2}:
        print(
            "usage: validate_compose.py RENDERED_COMPOSE_JSON [MOUNT_POLICY_JSON]",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SecurityValidationError("rendered Compose document must be an object")
    expected_mounts = None
    if len(arguments) == 2:
        policy = json.loads(Path(arguments[1]).read_text(encoding="utf-8"))
        expected_mounts = _mount_policy(policy)
    validate_compose(payload, expected_mounts=expected_mounts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
