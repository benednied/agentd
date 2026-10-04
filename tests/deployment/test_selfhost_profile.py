from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path

from test_container_security import _rendered_service

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_selfhost_profile_has_separate_service_state_and_repository() -> None:
    result = subprocess.run(
        [
            "sh",
            "-ec",
            'SCRIPT_DIR="$1/scripts"; . "$SCRIPT_DIR/common.sh"; '
            'printf "%s\\n" "$AGENTD_SERVICE" "$AGENTD_COMPOSE_PROJECT" '
            '"$AGENTD_REPOSITORY_ROOT" "$AGENTD_STATE_ROOT" "$AGENTD_SHARE_ROOT"',
            "profile-test",
            str(DEPLOY),
        ],
        env={**os.environ, "AGENTD_PROFILE": "selfhost"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "agentd-selfhost.service",
        "agentd-selfhost",
        "/home/bened/agentd",
        "/home/bened/.local/state/agentd-selfhost",
        "/home/bened/.local/share/agentd-selfhost",
    ]


def test_selfhost_policy_passes_validator_without_broadening_mounts(
    tmp_path: Path,
) -> None:
    import sys

    policy_path = DEPLOY / "security" / "selfhost-mount-policy.json"
    mounts = json.loads(policy_path.read_text())["mounts"]
    service = _rendered_service()
    for mount in service["volumes"]:
        mount["source"] = mounts[mount["target"]]
    rendered = tmp_path / "compose.json"
    rendered.write_text(json.dumps({"services": {"agentd": service}}))
    subprocess.run(
        [
            sys.executable,
            str(DEPLOY / "security" / "validate_compose.py"),
            str(rendered),
            str(policy_path),
        ],
        check=True,
    )
    assert mounts["/home/bened/goldenage"] == "/home/bened/agentd"
    assert len(mounts) == 6


def test_selfhost_generation_retains_configured_attempt_and_spending_limits(tmp_path):
    configure = runpy.run_path(str(DEPLOY.parent / "tools/configure_selfhost.py"))[
        "configure"
    ]
    share = tmp_path / ".local/share/agentd-selfhost"
    configure(tmp_path, "a" * 40)
    controller = share / "controller.json"
    initial = json.loads(controller.read_text())
    assert initial["maximum_total_attempts"] == (
        initial["maximum_automatic_attempts"] + initial["maximum_preparation_attempts"]
    )
    policy = {
        "maximum_automatic_attempts": 3,
        "maximum_preparation_attempts": 4,
        "maximum_total_attempts": 6,
        "maximum_tokens": 123456,
        "local_allowance": {
            "policy_id": "retained",
            "tokens_per_window": 234567,
            "window_seconds": 86400,
        },
    }
    initial.update(policy)
    controller.write_text(json.dumps(initial))
    configure(tmp_path, "b" * 40)
    for name in ("controller.json", "publisher.json"):
        regenerated = json.loads((share / name).read_text())
        assert {key: regenerated[key] for key in policy} == policy
        assert (
            regenerated["standing_github_policy"] == initial["standing_github_policy"]
        )
