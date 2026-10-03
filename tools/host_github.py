"""Run GitHub CLI from the current image with a protected host credential mount.

The release supervisor owns this entrypoint. It is never mounted in the worker
and never receives model-produced arguments. No credential is placed in argv
or a process environment; gh reads its own mode-0600 host configuration.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def main() -> None:
    arguments = sys.argv[1:]
    publish = arguments[:1] == ["--publish"]
    if publish:
        arguments.pop(0)
    share = Path("/home/bened/.local/share/agentd-selfhost")
    current = share / "coding-current"
    image_lines = [
        line
        for line in (current / "release.env").read_text().splitlines()
        if line.startswith("AGENTD_IMAGE=")
    ]
    match = re.fullmatch(
        r"AGENTD_IMAGE=(agentd-selfhost:[0-9a-f]{40})",
        image_lines[0] if len(image_lines) == 1 else "",
    )
    if match is None:
        raise ValueError("Expected a SHA-pinned self-host image")
    role = "publish" if publish else "read"
    configuration = (
        Path("/home/bened/.local/state/agentd-selfhost/coding") / f"github-{role}"
    )
    argv = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "-i",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--entrypoint",
        "/usr/bin/gh",
        "--env",
        "GH_CONFIG_DIR=/run/gh",
        "--env",
        "GH_PROMPT_DISABLED=1",
        "--mount",
        f"type=bind,src={configuration},dst=/run/gh,readonly",
        match.group(1),
        *arguments,
    ]
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
