"""Static checks for the dedicated Linux coding worker deployment."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _compose() -> dict:
    return json.loads((ROOT / "deploy/compose.coding.yaml").read_text())


def test_worker_profile_file_is_reachable_inside_the_worker_mount():
    """The worker command must consume a file from its mounted state volume."""
    worker = _compose()["services"]["coding-worker"]
    profile_arg = worker["command"][worker["command"].index("--profiles") + 1]
    state_mount = next(
        volume
        for volume in worker["volumes"]
        if volume["target"] == "/home/bened/.local/state/agentd"
    )
    assert profile_arg.startswith(state_mount["target"] + "/")
    relative = Path(profile_arg).relative_to(state_mount["target"])
    env = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (ROOT / "deploy/env/coding-worker.env.example")
        .read_text()
        .splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert env["AGENTD_CODING_PROFILES"] == str(
        Path(env["AGENTD_WORKER_STATE_ROOT"]) / relative
    )
    assert env["AGENTD_CODING_WORKER_PSK"].endswith("/coding-transport/worker.psk")


def test_worker_tls_is_internal_only_and_controller_has_no_worker_secret_mount():
    services = _compose()["services"]
    assert "ports" not in services["coding-worker"]
    assert "38101" in services["coding-worker"]["expose"]
    controller_targets = {
        item["target"] for item in services["coding-controller"]["volumes"]
    }
    assert "/home/bened/.local/state/agentd/worker.psk" not in controller_targets


def test_runtime_image_contains_worker_entrypoint_and_gh_client():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "apt-get install" in dockerfile and "gh" in dockerfile
    assert "tools/serve_coding_worker.py" in dockerfile
    assert "USER 1000:1000" in dockerfile


def test_compose_duration_uses_docker_units():
    for service in _compose()["services"].values():
        value = service["stop_grace_period"]
        assert value[-1:] in {"s", "m", "h"}, value


def test_coding_units_use_the_optional_host_override_wrapper():
    wrapper = ROOT / "deploy/scripts/coding-compose.sh"
    text = wrapper.read_text()
    assert "coding.override.yaml" in text
    assert '--file "$override"' in text
    for name in (
        "agentd-coding-controller.service",
        "agentd-worker.service",
        "agentd-publisher.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text()
        assert "coding-compose.sh" in unit
        assert "/usr/bin/docker compose" not in unit


def test_worker_has_private_runtime_and_uv_cache_mounts():
    worker = _compose()["services"]["coding-worker"]
    tmpfs = set(worker["tmpfs"])
    assert any(
        item.startswith("/run/agentd:") and "mode=0700" in item for item in tmpfs
    )
    assert any(
        item.startswith("/home/bened/.cache/uv:") and "mode=0700" in item
        for item in tmpfs
    )


def test_example_quota_commands_use_the_controller_database_mount():
    from agentd.cli import build_parser

    for role in ("coding-controller", "coding-publisher"):
        config = json.loads((ROOT / "deploy/examples" / f"{role}.json").read_text())
        args = build_parser().parse_args(config["quota_command"][1:])
        assert args.command == "codex-status"
        assert args.pool == config["account_pool"]
        assert args.db == config["database"]
        mounts = _compose()["services"][role]["volumes"]
        mount = next(
            m
            for m in mounts
            if m["source"].startswith("${AGENTD_CONTROLLER_STATE_ROOT:")
        )
        assert Path(args.db).is_relative_to(mount["target"])
