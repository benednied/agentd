from __future__ import annotations

import json
import os
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
