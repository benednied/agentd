from __future__ import annotations

import copy
import json
import runpy
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
DEPLOY = REPOSITORY / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
SECCOMP = DEPLOY / "container" / "seccomp-agentd.json"
ENV_TEMPLATE = DEPLOY / "env" / "agentd.env.example"
EXPECTED_MOUNTS = {
    "/home/bened/.local/state/agentd",
    "/home/bened/.local/share/agentd/workspaces",
    "/home/bened/.local/share/agentd/codex-home",
    "/home/bened/.cache/uv",
    "/home/bened/goldenage",
}
SERVICE_CONFIG_ENV = {
    "AGENTD_DB",
    "AGENTD_WORKSPACE_ROOT",
    "AGENTD_CODEX_HOME",
    "AGENTD_CODEX_MODEL",
    "AGENTD_CODEX_REASONING_EFFORT",
    "AGENTD_POLL_SECONDS",
    "AGENTD_ACCOUNT_POLL_SECONDS",
    "AGENTD_ACCOUNT_STALE_SECONDS",
    "AGENTD_QUOTA_TOP_UP_TOKENS",
    "AGENTD_HARD_CAP_GRACE_SECONDS",
    "UV_CACHE_DIR",
}


def _compose_service() -> dict[str, object]:
    payload = json.loads(COMPOSE.read_text(encoding="utf-8"))
    return payload["services"]["agentd"]


def _template_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        assert separator
        result[name] = value
    return result


def test_compose_enforces_resource_and_privilege_boundary() -> None:
    service = _compose_service()

    assert service["user"] == "1000:1000"
    assert service["read_only"] is True
    assert service["privileged"] is False
    assert service["cap_drop"] == ["ALL"]
    assert service["pids_limit"] == 512
    assert float(service["cpus"]) == 6
    assert service["mem_limit"] == "12g"
    assert service["pull_policy"] == "never"
    assert service["working_dir"] == "/home/bened/goldenage"
    assert service["init"] is True
    assert "no-new-privileges:true" in service["security_opt"]
    assert "apparmor=lxc-usernsexec" in service["security_opt"]
    assert "seccomp=./container/seccomp-agentd.json" in service["security_opt"]
    assert "ports" not in service


def test_compose_has_only_exact_narrow_mounts_and_tmpfs() -> None:
    service = _compose_service()
    volumes = service["volumes"]
    targets = {volume["target"] for volume in volumes}

    assert targets == EXPECTED_MOUNTS
    assert all(volume["type"] == "bind" for volume in volumes)
    assert all(volume["read_only"] is False for volume in volumes)
    assert all(volume["bind"] == {"create_host_path": False} for volume in volumes)
    serialized = json.dumps(volumes).lower()
    assert "docker.sock" not in serialized
    assert '"target": "/home/bened"' not in serialized
    assert '"target": "/home"' not in serialized
    assert '"target": "/"' not in serialized
    assert {item.split(":", 1)[0] for item in service["tmpfs"]} == {
        "/tmp",
        "/run/agentd",
    }
    assert all(
        option in " ".join(service["tmpfs"])
        for option in ("noexec", "nosuid", "nodev", "uid=1000", "gid=1000")
    )


def test_compose_aligns_service_config_and_nonsecret_git_identity() -> None:
    environment = _compose_service()["environment"]

    assert set(environment) >= SERVICE_CONFIG_ENV
    assert environment["AGENTD_DB"] == ("/home/bened/.local/state/agentd/state.sqlite")
    assert environment["AGENTD_WORKSPACE_ROOT"] == (
        "/home/bened/.local/share/agentd/workspaces"
    )
    assert environment["AGENTD_CODEX_HOME"] == (
        "/home/bened/.local/share/agentd/codex-home"
    )
    assert environment["CODEX_HOME"] == environment["AGENTD_CODEX_HOME"]
    assert environment["UV_CACHE_DIR"] == "/home/bened/.cache/uv"
    assert environment["GIT_AUTHOR_NAME"] == "agentd automation"
    assert environment["GIT_AUTHOR_EMAIL"] == "agentd@localhost"
    assert environment["GIT_COMMITTER_NAME"] == "agentd automation"
    assert environment["GIT_COMMITTER_EMAIL"] == "agentd@localhost"


def test_rendered_compose_security_validator_accepts_reviewed_contract(
    tmp_path: Path,
) -> None:
    payload = json.loads(COMPOSE.read_text(encoding="utf-8"))
    service = copy.deepcopy(payload["services"]["agentd"])
    service["image"] = f"agentd:{'a' * 40}"
    for volume in service["volumes"]:
        volume["source"] = volume["target"]
    for name, value in list(service["environment"].items()):
        if str(value).startswith("${"):
            service["environment"][name] = {
                "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
                "AGENTD_CODEX_REASONING_EFFORT": "medium",
            }.get(name, "1")
    rendered = tmp_path / "compose.json"
    rendered.write_text(
        json.dumps({"services": {"agentd": service}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(DEPLOY / "security" / "validate_compose.py"), rendered],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_rendered_compose_security_validator_rejects_unreviewed_exposure(
    tmp_path: Path,
) -> None:
    for case in ("environment", "mount"):
        payload = json.loads(COMPOSE.read_text(encoding="utf-8"))
        service = copy.deepcopy(payload["services"]["agentd"])
        service["image"] = f"agentd:{'a' * 40}"
        for volume in service["volumes"]:
            volume["source"] = volume["target"]
        for name, value in list(service["environment"].items()):
            if str(value).startswith("${"):
                service["environment"][name] = {
                    "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
                    "AGENTD_CODEX_REASONING_EFFORT": "medium",
                }.get(name, "1")
        if case == "environment":
            service["environment"]["UNREVIEWED_SECRET"] = "forbidden"
        else:
            service["volumes"][0]["source"] = "/home/bened"
        rendered = tmp_path / f"compose-{case}.json"
        rendered.write_text(
            json.dumps({"services": {"agentd": service}}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(DEPLOY / "security" / "validate_compose.py"),
                rendered,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0


def test_seccomp_is_default_deny_with_narrow_user_namespace_escape() -> None:
    profile = json.loads(SECCOMP.read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultErrnoRet"] == 1
    rules = profile["syscalls"]
    allowed = {
        name
        for rule in rules
        if rule["action"] == "SCMP_ACT_ALLOW"
        for name in rule["names"]
    }

    assert {
        "clone",
        "clone3",
        "unshare",
        "setns",
        "mount",
        "mknod",
        "rmdir",
        "umount2",
        "signalfd4",
    } <= allowed
    assert {
        "add_key",
        "bpf",
        "delete_module",
        "finit_module",
        "init_module",
        "kexec_load",
        "keyctl",
        "open_by_handle_at",
        "perf_event_open",
        "ptrace",
        "reboot",
        "request_key",
        "swapon",
    }.isdisjoint(allowed)
    namespace_rules = [
        rule for rule in rules if rule["names"] in (["clone", "unshare"], ["setns"])
    ]
    assert all(rule.get("args") for rule in namespace_rules)
    assert any(
        argument.get("value") == 100663424 and argument.get("valueTwo") == 0
        for rule in namespace_rules
        for argument in rule["args"]
    )
    assert any(
        argument.get("value") == 268435456
        for rule in rules
        if rule["names"] == ["setns"]
        for argument in rule.get("args", [])
    )


def test_dockerfile_keeps_runtime_tools_and_never_embeds_credentials() -> None:
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPOSITORY / ".dockerignore").read_text(encoding="utf-8")

    assert "USER 1000:1000" in dockerfile
    assert 'ENTRYPOINT ["/opt/agentd/venv/bin/agentd"]' in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    assert "bubblewrap" in dockerfile
    assert "uidmap" in dockerfile
    assert "FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-runtime" in dockerfile
    assert "COPY --from=uv-runtime /uv /usr/local/bin/uv" in dockerfile
    assert "COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv" in dockerfile
    assert 'UV_CACHE_DIR="/home/bened/.cache/uv"' in dockerfile
    assert 'PATH="/usr/local/libexec/agentd:' in dockerfile
    assert "deploy/container/bwrap /usr/local/libexec/agentd/bwrap" in dockerfile
    assert "runtime_sandbox_probe.py" in dockerfile
    assert "sandbox_payload.py" in dockerfile
    assert "auth.json" not in dockerfile
    assert "docker.sock" not in dockerfile
    assert ".idea" in dockerignore
    assert "auth.json" in dockerignore
    assert ".codex" in dockerignore


def test_env_template_has_exact_nonsecret_mount_and_service_values() -> None:
    environment = _template_environment()

    assert set(environment) >= SERVICE_CONFIG_ENV
    assert environment["AGENTD_STATE_ROOT"] == "/home/bened/.local/state/agentd"
    assert environment["AGENTD_WORKSPACE_ROOT"] == (
        "/home/bened/.local/share/agentd/workspaces"
    )
    assert environment["AGENTD_CODEX_HOME"] == (
        "/home/bened/.local/share/agentd/codex-home"
    )
    assert environment["AGENTD_UV_CACHE"] == "/home/bened/.cache/uv"
    assert environment["AGENTD_REPOSITORY_ROOT"] == "/home/bened/goldenage"
    assert "AUTH" not in " ".join(environment)
    assert all("secret" not in value.lower() for value in environment.values())


def test_provisioning_and_release_scripts_are_syntax_checked_and_local_only() -> None:
    scripts = sorted((DEPLOY / "scripts").glob("*.sh"))
    assert scripts
    for script in scripts:
        result = subprocess.run(
            ["sh", "-n", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"

    combined = "\n".join(script.read_text(encoding="utf-8") for script in scripts)
    assert all(command not in combined for command in ("scp ", "ssh ", "rsync "))
    assert "validate_sha" in combined
    assert "git -C" in combined and "archive --format=tar" in combined
    assert "source_repository=${2:-$AGENTD_SOURCE_ROOT}" in combined
    assert "backup_state" in combined and "restore_state" in combined
    assert "sqlite_snapshot.py" in combined
    assert "require_command sqlite3" not in combined
    assert '"$AGENTD_DB-wal"' in combined
    assert '"$AGENTD_DB-shm"' in combined
    assert "AGENTD_AUTH_SOURCE" in combined
    assert '"$AGENTD_AUTH_SOURCE" "$auth_tmp"' in combined
    assert "-m 0600" in combined and "-m 0700" in combined
    assert "systemctl --user" in combined
    assert '"$staging/deploy/scripts/check-container-security.sh"' in combined
    container_check = (DEPLOY / "scripts" / "check-container-security.sh").read_text(
        encoding="utf-8"
    )
    assert "/opt/agentd/security/runtime_sandbox_probe.py" in container_check


def test_sqlite_snapshot_round_trip(tmp_path: Path) -> None:
    import sqlite3

    source = tmp_path / "source.sqlite"
    snapshot = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_table VALUES ('durable')")

    tool = DEPLOY / "security" / "sqlite_snapshot.py"
    subprocess.run(
        [sys.executable, str(tool), "backup", str(source), str(snapshot)],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(tool), "verify", str(snapshot)],
        check=True,
    )
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("SELECT value FROM values_table").fetchone() == (
            "durable",
        )


def test_codex_config_is_explicit_nonsecret_and_provisioned_mode_0600() -> None:
    config_path = DEPLOY / "container" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provision = (DEPLOY / "scripts" / "provision-host.sh").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "workspace-write"
    assert config["sandbox_workspace_write"] == {
        "network_access": False,
        "exclude_tmpdir_env_var": True,
        "exclude_slash_tmp": True,
    }
    assert config["analytics"] == {"enabled": False}
    assert "auth" not in config_path.read_text(encoding="utf-8").lower()
    assert "../container/config.toml" in provision
    assert 'install -o 1000 -g 1000 -m 0600 "$config_source"' in provision
    assert 'chmod 0600 "$AGENTD_CODEX_HOME/config.toml"' in provision
    assert "deploy/container/config.toml /opt/agentd/security/config.toml" in dockerfile


def test_bwrap_wrapper_masks_sensitive_roots_and_forces_network_namespace() -> None:
    wrapper_path = DEPLOY / "container" / "bwrap"
    wrapper = wrapper_path.read_text(encoding="utf-8")

    assert 'REAL_BWRAP = "/usr/bin/bwrap"' in wrapper
    assert 'CODEX_HOME = "/home/bened/.local/share/agentd/codex-home"' in wrapper
    assert 'STATE_HOME = "/home/bened/.local/state/agentd"' in wrapper
    assert '"--unshare-net"' in wrapper
    assert wrapper.count('"--tmpfs"') == 2
    assert 'arguments.index("--")' in wrapper
    assert "*arguments[:command_separator]" in wrapper
    assert "*arguments[command_separator:]" in wrapper
    assert 'if "--share-net" in arguments[:command_separator]' in wrapper
    assert "os.execv(" in wrapper and "REAL_BWRAP" in wrapper
    assert "AGENTD_BWRAP_AUDIT_FILE" in wrapper

    wrapped_argv = cast(
        Callable[[list[str]], list[str]],
        runpy.run_path(str(wrapper_path))["_wrapped_argv"],
    )
    generated = wrapped_argv(["--ro-bind", "/", "/", "--", "/bin/true"])
    separator = generated.index("--")
    assert generated[0] == "/usr/bin/bwrap"
    assert generated[separator - 5 : separator] == [
        "--unshare-net",
        "--tmpfs",
        "/home/bened/.local/share/agentd/codex-home",
        "--tmpfs",
        "/home/bened/.local/state/agentd",
    ]
    assert generated[separator:] == ["--", "/bin/true"]
    with pytest.raises(SystemExit, match="refuses --share-net"):
        wrapped_argv(["--share-net", "--", "/bin/true"])


def test_runtime_preflight_exercises_direct_and_pinned_codex_sandboxes() -> None:
    preflight = (DEPLOY / "security" / "runtime_sandbox_probe.py").read_text(
        encoding="utf-8"
    )
    payload = (DEPLOY / "security" / "sandbox_payload.py").read_text(encoding="utf-8")

    assert 'EXPECTED_SDK_VERSION = "0.144.4"' in preflight
    assert "bundled_codex_path" in preflight
    assert "launch_args_override" not in preflight
    assert "CommandExecParams" in preflight
    assert '"command/exec"' in preflight
    assert '"type": "workspaceWrite"' in preflight
    assert '"writableRoots": [str(worktree)]' in preflight
    assert '"networkAccess": False' in preflight
    assert "_run_direct_bwrap_probe(worktree, audit_file)" in preflight
    assert '"--dev-bind"' in preflight
    assert "_run_codex_generated_command_probe(worktree, audit_file)" in preflight
    assert "_require_wrapper_audit" in preflight
    assert "result.exit_code != 0" in preflight
    assert "/home/bened/.local/share/agentd/codex-home/auth.json" in payload
    assert "/home/bened/.local/state/agentd/state.sqlite" in payload
    assert 'REPOSITORY_ROOT = Path("/home/bened/goldenage")' in payload
    assert 'UV_CACHE_ROOT = Path("/home/bened/.cache/uv")' in payload
    assert "_unexpectedly_writable" in payload
    assert "os.O_WRONLY | os.O_CREAT | os.O_EXCL" in payload
    assert "socket.SOCK_DGRAM" in payload
    assert '("192.0.2.1", 9)' in payload
    assert "errno.ENETUNREACH" in payload
    assert 'Path("/usr/bin/bwrap")' in preflight
    assert "metadata.st_uid != 0" in preflight


def test_user_systemd_unit_uses_versioned_current_release_and_hardening() -> None:
    unit = (DEPLOY / "systemd" / "agentd.service").read_text(encoding="utf-8")

    assert "/home/bened/.local/share/agentd/current/deploy/compose.yaml" in unit
    assert "/home/bened/.local/share/agentd/current/release.env" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "PrivateDevices=yes" not in unit
    assert "--remove-orphans --wait" in unit
    assert "check-container-security.sh" in unit
    assert "runtime" in unit
    assert "User=root" not in unit
