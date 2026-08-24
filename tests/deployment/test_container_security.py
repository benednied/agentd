from __future__ import annotations

import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

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
    "/home/bened/.local/share/agentd/codex-home/config.toml",
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
    "AGENTD_LOG_LEVEL",
    "AGENTD_LOG_FORMAT",
    "UV_CACHE_DIR",
}


def _compose_service() -> dict[str, object]:
    payload = json.loads(COMPOSE.read_text(encoding="utf-8"))
    return payload["services"]["agentd"]


def _rendered_service() -> dict[str, object]:
    service = copy.deepcopy(_compose_service())
    service["image"] = f"agentd:{'a' * 40}"
    service["entrypoint"][2] = service["entrypoint"][2].replace("$", "$$")
    for volume in service["volumes"]:
        volume["source"] = volume["target"]
    for name, value in list(service["environment"].items()):
        if str(value).startswith("${"):
            service["environment"][name] = {
                "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
                "AGENTD_CODEX_REASONING_EFFORT": "medium",
            }.get(name, "1")
    return service


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


def test_compose_runs_runtime_preflight_inside_every_container_start() -> None:
    service = _compose_service()

    assert service["restart"] == "unless-stopped"
    assert service["entrypoint"] == [
        "/bin/sh",
        "-ec",
        (
            "/opt/agentd/venv/bin/python "
            "/opt/agentd/security/runtime_sandbox_probe.py && "
            'exec /opt/agentd/venv/bin/agentd "$@"'
        ),
        "agentd-entrypoint",
    ]


def test_compose_has_only_exact_narrow_mounts_and_tmpfs() -> None:
    service = _compose_service()
    volumes = service["volumes"]
    targets = {volume["target"] for volume in volumes}

    assert targets == EXPECTED_MOUNTS
    assert all(volume["type"] == "bind" for volume in volumes)
    assert {volume["target"] for volume in volumes if volume["read_only"] is True} == {
        "/home/bened/.local/share/agentd/codex-home/config.toml"
    }
    assert all(
        volume["read_only"] is False
        for volume in volumes
        if volume["target"] != "/home/bened/.local/share/agentd/codex-home/config.toml"
    )
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
    service = _rendered_service()
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


@pytest.mark.parametrize("case", ["seccomp", "tmpfs", "entrypoint"])
def test_rendered_validator_rejects_security_preflight_bypasses(
    tmp_path: Path,
    case: str,
) -> None:
    service = _rendered_service()
    if case == "seccomp":
        service["security_opt"][-1] = "seccomp=/tmp/unreviewed/seccomp-agentd.json"
    elif case == "tmpfs":
        service["tmpfs"][0] = (
            service["tmpfs"][0]
            .replace("noexec,", "")
            .replace(
                ",size=1073741824",
                "",
            )
        )
    else:
        service["entrypoint"] = ["/opt/agentd/venv/bin/agentd"]
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


def test_rendered_validator_requires_explicit_exact_alternate_mount_policy(
    tmp_path: Path,
) -> None:
    payload = json.loads(COMPOSE.read_text(encoding="utf-8"))
    service = copy.deepcopy(payload["services"]["agentd"])
    service["image"] = f"agentd:{'a' * 40}"
    alternate_sources = {
        target: f"/srv/agentd-smoke/{index}"
        for index, target in enumerate(sorted(EXPECTED_MOUNTS), start=1)
    }
    for volume in service["volumes"]:
        volume["source"] = alternate_sources[volume["target"]]
    for name, value in list(service["environment"].items()):
        if str(value).startswith("${"):
            service["environment"][name] = {
                "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
                "AGENTD_CODEX_REASONING_EFFORT": "medium",
            }.get(name, "1")
    rendered = tmp_path / "compose-alternate.json"
    rendered.write_text(
        json.dumps({"services": {"agentd": service}}),
        encoding="utf-8",
    )
    validator = DEPLOY / "security" / "validate_compose.py"

    without_policy = subprocess.run(
        [sys.executable, str(validator), rendered],
        check=False,
        capture_output=True,
        text=True,
    )
    assert without_policy.returncode != 0

    policy = tmp_path / "mount-policy.json"
    policy.write_text(json.dumps({"mounts": alternate_sources}), encoding="utf-8")
    with_policy = subprocess.run(
        [sys.executable, str(validator), rendered, policy],
        check=False,
        capture_output=True,
        text=True,
    )
    assert with_policy.returncode == 0, with_policy.stderr

    policy.write_text(
        json.dumps(
            {
                "mounts": {
                    **alternate_sources,
                    "/home/bened/goldenage": "/home/bened",
                }
            }
        ),
        encoding="utf-8",
    )
    broad_policy = subprocess.run(
        [sys.executable, str(validator), rendered, policy],
        check=False,
        capture_output=True,
        text=True,
    )
    assert broad_policy.returncode != 0


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
        "fgetxattr",
        "flistxattr",
        "fremovexattr",
        "fsetxattr",
        "getxattr",
        "lgetxattr",
        "listxattr",
        "llistxattr",
        "lremovexattr",
        "lsetxattr",
        "removexattr",
        "setxattr",
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
    assert "ENV UV_PROJECT_ENVIRONMENT=/opt/agentd/venv" in dockerfile
    assert "COPY --from=builder /opt/agentd/venv /opt/agentd/venv" in dockerfile
    assert "COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv" in dockerfile
    assert 'UV_CACHE_DIR="/home/bened/.cache/uv"' in dockerfile
    assert 'PATH="/opt/agentd/venv/bin:' in dockerfile
    assert "deploy/container/bwrap_compat.c" in dockerfile
    assert "FROM ${PYTHON_IMAGE} AS bwrap-compat-builder" in dockerfile
    assert "-Wl,-z,relro,-z,now" in dockerfile
    assert "mv /usr/bin/bwrap /usr/libexec/agentd/bwrap.real" in dockerfile
    assert "/build/bwrap /usr/bin/bwrap" in dockerfile
    assert "--chown=0:0 --chmod=0555" in dockerfile
    assert "/usr/libexec/agentd/codex-linux-sandbox" in dockerfile
    assert "/usr/libexec/agentd/codex-execve-wrapper" in dockerfile
    assert "/usr/libexec/agentd/apply_patch" in dockerfile
    assert "/usr/libexec/agentd/applypatch" in dockerfile
    assert 'PATH="/opt/agentd/venv/bin:/usr/libexec/agentd:' in dockerfile
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
    assert environment["AGENTD_CODEX_CONFIG"] == (
        "/home/bened/.local/share/agentd/codex-home/config.toml"
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
    common = (DEPLOY / "scripts" / "common.sh").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

    assert config["approval_policy"] == "never"
    assert config["default_permissions"] == "agentd-workspace"
    assert "sandbox_mode" not in config
    assert "sandbox_workspace_write" not in config
    profile = config["permissions"]["agentd-workspace"]
    assert profile["description"] == "Agentd leased workspace only"
    filesystem = profile["filesystem"]
    assert filesystem[":minimal"] == "read"
    assert filesystem[":workspace_roots"] == {".": "write"}
    for root in (
        "/opt/agentd/venv",
        "/usr/local/bin",
        "/usr/bin",
        "/usr/libexec/agentd",
        "/bin",
        "/usr/lib",
        "/lib",
    ):
        assert filesystem[root] == "read"
    codex_home = "/home/bened/.local/share/agentd/codex-home"
    assert filesystem[codex_home] == "deny"
    assert {
        path: access
        for path, access in filesystem.items()
        if path.startswith(f"{codex_home}/")
    } == {}
    assert f"{codex_home}/auth.json" not in filesystem
    assert filesystem["/home/bened/.local/state/agentd"] == "deny"
    assert filesystem["/home/bened/.local/share/agentd/workspaces"] == "deny"
    assert profile["network"] == {"enabled": False}
    assert config["analytics"] == {"enabled": False}
    assert "auth" not in config_path.read_text(encoding="utf-8").lower()
    assert "../container/config.toml" in provision
    assert 'install_config_atomically "$config_source"' in provision
    assert "require_safe_codex_config" in provision
    assert 'install -o 1000 -g 1000 -m 0600 "$config_source"' in common
    assert 'mv -f "$config_tmp" "$AGENTD_CODEX_CONFIG"' in common
    assert "deploy/container/config.toml /opt/agentd/security/config.toml" in dockerfile


def test_release_backup_and_rollback_include_codex_policy() -> None:
    common = (DEPLOY / "scripts" / "common.sh").read_text(encoding="utf-8")
    deploy = (DEPLOY / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    rollback = (DEPLOY / "scripts" / "rollback.sh").read_text(encoding="utf-8")
    provision = (DEPLOY / "scripts" / "provision-host.sh").read_text(encoding="utf-8")

    assert '"$AGENTD_CODEX_CONFIG" "$backup_dir/config.toml"' in common
    assert 'cmp -s "$AGENTD_CODEX_CONFIG" "$release_config"' in common
    assert '"$backup_dir/config.toml" \\' in common
    assert "config_restore_tmp=$AGENTD_CODEX_HOME/.config.toml.restore-$$" in common
    assert 'mv -f "$config_restore_tmp" "$AGENTD_CODEX_CONFIG"' in common
    assert 'install_release_config "$release_sha"' in deploy
    assert 'if ! atomic_release_link "$release_sha"; then' in deploy
    assert deploy.count("restore_previous_activation") == 4
    assert deploy.index("stop_current_release_containers") < deploy.index(
        'restore_state "$backup_dir"'
    )
    assert 'restore_state "$backup_dir"' in deploy
    assert 'if ! atomic_release_link "$target_sha"; then' in rollback
    assert rollback.count("restore_pre_rollback_activation") == 3
    assert rollback.index("stop_current_release_containers") < rollback.index(
        'restore_state "$safety_backup"'
    )
    assert 'restore_state "$selected_backup"' in rollback
    assert 'cmp -s "$selected_backup/config.toml" "$target_config"' in rollback
    assert 'restore_state "$safety_backup"' in rollback
    assert 'if ! mv -Tf "$temporary_link" "$AGENTD_CURRENT_LINK"; then' in common
    assert 'if ! mv -f "$restore_tmp" "$AGENTD_DB"; then' in common
    assert common.index(
        'mv -f "$config_restore_tmp" "$AGENTD_CODEX_CONFIG"'
    ) < common.index('mv -f "$restore_tmp" "$AGENTD_DB"')
    assert "config_status=preserved" in provision


def test_codex_config_pins_repository_trust_before_read_only_mount() -> None:
    config = tomllib.loads(
        (DEPLOY / "container" / "config.toml").read_text(encoding="utf-8")
    )

    assert config["projects"]["/home/bened/goldenage"]["trust_level"] == "trusted"
    service = _compose_service()
    config_mount = next(
        volume
        for volume in service["volumes"]
        if volume["target"] == "/home/bened/.local/share/agentd/codex-home/config.toml"
    )
    assert config_mount["read_only"] is True
    assert config_mount["bind"] == {"create_host_path": False}


def test_runtime_preflight_exercises_direct_and_pinned_codex_sandboxes() -> None:
    preflight = (DEPLOY / "security" / "runtime_sandbox_probe.py").read_text(
        encoding="utf-8"
    )
    payload = (DEPLOY / "security" / "sandbox_payload.py").read_text(encoding="utf-8")

    assert 'EXPECTED_SDK_VERSION = "0.144.4"' in preflight
    assert "bundled_codex_path" in preflight
    assert "launch_args_override" not in preflight
    assert "CommandExecParams" not in preflight
    assert "PermissionProfileListResponse" in preflight
    assert "cwd=str(worktree)" in preflight
    assert '"permissionProfile/list"' in preflight
    assert '"command/exec"' in preflight
    assert '"permissionProfile": PERMISSION_PROFILE' in preflight
    assert '"sandboxPolicy"' not in preflight
    assert "_run_direct_bwrap_probe(worktree)" in preflight
    assert '"--unshare-net"' in preflight
    assert preflight.count('"--tmpfs"') == 2
    assert '"--dev"' in preflight
    assert 'REAL_BWRAP = Path("/usr/libexec/agentd/bwrap.real")' in preflight
    assert "_run_shim_rejection_probes()" in preflight
    assert "_require_bwrap_rewrite_audit" in preflight
    assert 'fields.get("rewrite_dev") == "1"' in preflight
    assert 'fields.get("rewrite_helper") == "1"' in preflight
    assert 'fields.get("unshare_net") != "1"' in preflight
    assert "require_helper_rewrite=True" in preflight
    assert "allow_additional_device_rewrites=True" in preflight
    assert "CODEX_HELPER_ALIASES" in preflight
    assert 'AGENTD_ENTRYPOINT = Path("/opt/agentd/venv/bin/agentd")' in preflight
    assert 'b"#!/opt/agentd/venv/bin/python"' in preflight
    assert 'b"#!/opt/agentd/venv/bin/python3"' in preflight
    assert "secrets.token_hex(16)" in preflight
    assert "_run_codex_generated_command_probe(worktree)" in preflight
    assert "shutil.copyfile(PAYLOAD, workspace_payload)" in preflight
    assert "_create_toolchain_launcher(token)" in preflight
    assert "toolchain_launcher.unlink(missing_ok=True)" in preflight
    assert '"command": [str(toolchain_launcher), str(workspace_payload)]' in preflight
    assert "result.exit_code != 0" in preflight
    assert "/home/bened/.local/share/agentd/codex-home/auth.json" in payload
    assert 'ARG0_ROOT = AUTH_FILE.parent / "tmp/arg0"' in payload
    assert "_is_readable(ARG0_ROOT)" in payload
    assert "/home/bened/.local/state/agentd/state.sqlite" in payload
    assert 'REPOSITORY_ROOT = Path("/home/bened/goldenage")' in payload
    assert 'UV_CACHE_ROOT = Path("/home/bened/.cache/uv")' in payload
    assert "AUTH_FILE.parent" in payload
    assert "STATE_DATABASE.parent" in payload
    assert 'for helper in ("apply_patch", "applypatch")' in payload
    assert "_exercise_patch_helper(worktree, helper)" in payload
    assert "_unexpectedly_writable" in payload
    assert "os.O_WRONLY | os.O_CREAT | os.O_EXCL" in payload
    assert "socket.SOCK_DGRAM" in payload
    assert '("192.0.2.1", 9)' in payload
    assert "errno.ENETUNREACH" in payload
    assert 'Path("/usr/bin/bwrap")' in preflight
    assert "metadata.st_uid != 0" in preflight


def test_bwrap_compatibility_shim_is_narrow_and_fail_closed() -> None:
    shim = (DEPLOY / "container" / "bwrap_compat.c").read_text(encoding="utf-8")

    assert '#define REAL_BWRAP "/usr/libexec/agentd/bwrap.real"' in shim
    assert '#define AUDIT_DIRECTORY "/run/agentd"' in shim
    assert (
        '#define CODEX_SANDBOX_ALIAS "/usr/libexec/agentd/codex-linux-sandbox"' in shim
    )
    assert "valid_codex_sandbox_helper" in shim
    assert "unvalidated Codex-home command path rejected" in shim
    assert 'strcmp(argument, "--dev") == 0' in shim
    assert '(char *)"--dev-bind"' in shim
    assert 'rewritten[output++] = (char *)"--unshare-net"' not in shim
    assert 'strcmp(argv[index + 1], "/dev") != 0' in shim
    assert 'strcmp(argument, "--share-net") == 0' in shim
    assert 'strcmp(argument, "--args") == 0' in shim
    assert 'strcmp(argument, "--dev-bind-try") == 0' in shim
    assert "unknown Bubblewrap option rejected" in shim
    assert "inspection->unshare_net_seen ? 1 : 0" in shim
    assert "inspection->helper_rewrite_index >= 0 ? 1 : 0" in shim
    assert "O_NOFOLLOW" in shim
    assert "metadata.st_uid != 0" in shim
    assert "fexecve(real_bwrap, rewritten, environ)" in shim


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
