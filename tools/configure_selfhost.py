"""Write the nonsecret HP configuration for GitHub-only agentd operation.

Run on HP after preparing protected credential/transport directories. This
idempotent generator preserves the first activation timestamp and established
spending policy. It never reads or prints credentials.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def write(path: Path, data: dict | list | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = data if isinstance(data, str) else json.dumps(data, indent=2) + "\n"
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(text)
    temp.chmod(0o600)
    temp.replace(path)


def configure(home: Path, base_commit: str) -> None:
    share = home / ".local/share/agentd-selfhost"
    state = home / ".local/state/agentd-selfhost/coding"
    container_state = "/home/bened/.local/state/agentd"
    database = container_state + "/controller/state.sqlite"
    existing = share / "controller.json"
    prior = json.loads(existing.read_text()) if existing.exists() else {}
    activated = prior.get("standing_github_policy", {}).get(
        "activated_at", datetime.now(UTC).isoformat()
    )
    profile = {
        "id": "agentd-linux-v1",
        "version": "1",
        "repository": "benednied/agentd",
        "clone_url": "https://github.com/benednied/agentd.git",
        "harnesses": ["codex"],
        "required_capabilities": ["remote-coding"],
        "validation_commands": [
            [
                "/opt/agentd/validation-venv/bin/python",
                "-I",
                "/opt/agentd/tools/validate_python_package.py",
                "--python",
                "/opt/agentd/validation-venv/bin/python",
            ]
        ],
        "max_runtime_seconds": 3600,
        "validation_timeout_seconds": 900,
        "network_policy": "disabled",
        "preparation_strategy": "git-worktree",
        "filesystem_policy": "workspace-write",
    }
    config = {
        "profile": profile,
        "repository_id": 1328873039,
        "base_commit": base_commit,
        "base_branch": "master",
        "refresh_base_from_github": True,
        "account_pool": "codex",
        "eligibility_label": None,
        "standing_github_policy": {
            "activated_at": activated,
            "trusted_actors": {"benednied": 116355829},
        },
        "expected_tokens": 300000,
        "maximum_tokens": 1000000,
        "minimum_interactive_reserve": 250000,
        "local_allowance": prior.get(
            "local_allowance",
            {
                "policy_id": "hp-selfhost-daily-v1",
                "tokens_per_window": 2000000,
                "window_seconds": 86400,
            },
        ),
        "automatic_resume_budget": {
            "maximum_tokens": 2000000,
            "increment_tokens": 500000,
            "checkpoint_fraction": 0.9,
        },
        "maximum_automatic_attempts": 4,
        "maximum_preparation_attempts": 3,
        "maximum_total_attempts": None,
        "maximum_validation_attempts": 3,
        "automatic_validation_repair": True,
        "auto_resume_checkpoints": True,
        "allow_ready_pr_updates": True,
        "background_block_used_percent": 75,
        "urgent_only_used_percent": 90,
        "database": database,
        "object_cache": container_state + "/publication-cache/agentd.git",
        "unused_workspace_root": container_state + "/controller/unused-workspaces",
        "quota_command": [
            "/opt/agentd/venv/bin/agentd",
            "--db",
            database,
            "codex-status",
            "--pool",
            "codex",
        ],
        "poll_interval_seconds": 10,
        "source_poll_seconds": 60,
        "publication_enabled": False,
        "integration": {
            "enabled": True,
            "required_checks": ["quality"],
            "merge_method": "merge",
        },
        "validation_runtime_mounts": [
            "/opt/agentd/validation-venv",
            "/opt/agentd/tools",
            "/usr/local",
        ],
        "worker": {
            "name": "coding-worker",
            "host": "coding-worker",
            "port": 38101,
            "node_id": "agentd-selfhost-worker",
            "session_epoch": "selfhost-v1",
            "psk_file": container_state + "/transport/worker.psk",
            "tls_ca": container_state + "/transport/worker.crt",
            "server_hostname": "coding-worker",
            "features": ["coding-checkpoints"],
        },
    }
    # Existing host policy changes are administrative authority; generation
    # cannot silently reset trust, spending or approval gates.
    for key in (
        "standing_github_policy",
        "expected_tokens",
        "maximum_tokens",
        "minimum_interactive_reserve",
        "local_allowance",
        "automatic_resume_budget",
        "maximum_automatic_attempts",
        "maximum_preparation_attempts",
        "maximum_total_attempts",
        "maximum_validation_attempts",
        "background_block_used_percent",
        "urgent_only_used_percent",
        "integration",
    ):
        if key in prior:
            config[key] = prior[key]
    if config["maximum_total_attempts"] is None:
        config["maximum_total_attempts"] = (
            config["maximum_automatic_attempts"]
            + config["maximum_preparation_attempts"]
        )
    for name in (
        "controller",
        "worker",
        "transport",
        "github-read",
        "github-publish",
        "controller-codex",
        "worker-codex",
        "publication-cache",
    ):
        (state / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    workspaces = share / "coding-workspaces"
    workspaces.mkdir(parents=True, exist_ok=True, mode=0o700)
    write(existing, config)
    write(share / "publisher.json", config)
    write(state / "worker/coding-profiles.json", [profile])
    variables = {
        "AGENTD_CODING_COMPOSE_PROJECT": "agentd-selfhost-coding",
        "AGENTD_CONTROLLER_STATE_ROOT": state / "controller",
        "AGENTD_WORKER_STATE_ROOT": state / "worker",
        "AGENTD_CONTROLLER_CONFIG": share / "controller.json",
        "AGENTD_PUBLISHER_CONFIG": share / "publisher.json",
        "AGENTD_CONTROLLER_GH_CONFIG": state / "github-read",
        "AGENTD_PUBLISHER_GH_CONFIG": state / "github-publish",
        "AGENTD_CONTROLLER_TRANSPORT": state / "transport",
        "AGENTD_CONTROLLER_CODEX_HOME": state / "controller-codex",
        "AGENTD_WORKER_CODEX_HOME": state / "worker-codex",
        "AGENTD_PUBLICATION_CACHE": state / "publication-cache",
        "AGENTD_WORKSPACE_ROOT": workspaces,
        "AGENTD_CODEX_CONFIG": share / "coding-config.toml",
        "AGENTD_CODING_NODE_ID": "agentd-selfhost-worker",
        "AGENTD_CODING_SESSION_EPOCH": "selfhost-v1",
        "AGENTD_CODING_MODEL": "gpt-5.6-luna",
        "AGENTD_CODING_DEPENDENCY_VENV": "/opt/agentd/validation-venv",
        "AGENTD_CODING_DEPENDENCY_PROFILE": profile["id"],
        "AGENTD_CODING_DEPENDENCY_PYTHON_ROOT": "/usr/local",
    }
    write(share / "coding.env", "".join(f"{k}={v}\n" for k, v in variables.items()))
    # Worker bind root is protected state; the profiles file is already below it.
    write(
        share / "coding.override.yaml",
        {
            "services": {
                "coding-worker": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(state / "worker/coding-profiles.json"),
                            "target": container_state + "/coding-profiles.json",
                            "read_only": True,
                            "bind": {"create_host_path": False},
                        }
                    ],
                }
            }
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ["HOME"]))
    parser.add_argument("--base-commit", required=True)
    args = parser.parse_args()
    if len(args.base_commit) != 40 or any(
        c not in "0123456789abcdef" for c in args.base_commit
    ):
        parser.error("base commit must be a full SHA")
    configure(args.home.resolve(), args.base_commit)


if __name__ == "__main__":
    main()
