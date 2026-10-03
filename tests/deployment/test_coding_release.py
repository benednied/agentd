"""Execute release gates with isolated paths and recording host adapters."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.mark.parametrize(
    "case",
    ["inactive", "unreachable", "busy", "stale", "unresolved", "not_live", "idle"],
)
def test_upgrade_requires_live_controller_and_proven_idle_workers(tmp_path, case):
    home = tmp_path / "home"
    base = home / ".local/share/agentd"
    base.mkdir(parents=True)
    (base / "coding.env").write_text("")
    calls = tmp_path / "calls"
    calls.write_text("")
    health = {
        "controller_live": case != "not_live",
        "draining": True,
        "unresolved_runs": ["run"] if case == "unresolved" else [],
        "workers": [
            {"fresh": case != "stale", "active_runs": 1 if case == "busy" else 0}
        ],
    }
    health_file = tmp_path / "health.json"
    health_file.write_text(json.dumps(health))
    for name in ("old", "new"):
        release = base / name
        (release / "deploy/scripts").mkdir(parents=True)
        (release / "deploy/systemd").mkdir()
        (release / "release.env").write_text("")
        (release / "deploy/compose.coding.yaml").write_text("{}")
        for unit in ("agentd-coding-controller", "agentd-worker", "agentd-publisher"):
            (release / f"deploy/systemd/{unit}.service").write_text("[Service]\n")
        executable(
            release / "deploy/scripts/coding-compose.sh",
            """#!/bin/sh
printf 'compose %s\\n' "$*" >> "$CALLS"
case "$*" in
  *health) [ "$CASE" != unreachable ] || exit 1; cat "$HEALTH" ;;
esac
""",
        )
    current = base / "coding-current"
    current.symlink_to(base / "old")
    script = tmp_path / "release.sh"
    script.write_text(
        (ROOT / "deploy/scripts/coding-release.sh")
        .read_text()
        .replace("/home/bened", str(home))
    )
    commands = tmp_path / "bin"
    commands.mkdir()
    executable(
        commands / "systemctl",
        """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$CALLS"
case "$*" in
  *is-active*) [ "$CASE" != inactive ] ;;
  *) exit 0 ;;
esac
""",
    )
    # Linux uses readlink -f and mv -T; supply their exact required semantics
    # so the same behavioral regression also runs on macOS.
    executable(
        commands / "readlink",
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        "print(Path(sys.argv[-1]).resolve())\n",
    )
    executable(
        commands / "realpath",
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        "print(Path(sys.argv[-1]).resolve())\n",
    )
    executable(
        commands / "mv",
        f"#!{sys.executable}\nimport os,sys\nos.replace(sys.argv[-2],sys.argv[-1])\n",
    )
    result = subprocess.run(
        ["sh", str(script), "activate", str(base / "new")],
        env={
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls),
            "CASE": case,
            "HEALTH": str(health_file),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    log = calls.read_text()
    if case == "idle":
        assert result.returncode == 0, result.stderr
        assert "systemctl --user stop agentd-worker.service" in log
        assert current.resolve() == base / "new"
    else:
        assert result.returncode != 0
        assert "systemctl --user stop" not in log
        assert "systemctl --user start" not in log
        assert current.resolve() == base / "old"
    if case == "inactive":
        assert "health" not in log
        assert "restore its ownership" in result.stderr
