from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from qualify_coding_worker import parser


def test_persistent_parser_exposes_unbounded_lifetime_default():
    args = parser(include_lifetime=False).parse_args(
        [
            "--profiles",
            "profiles.json",
            "--account-pool",
            "pool",
            "--state-root",
            "state",
            "--workspace-root",
            "workspace",
            "--secret-file",
            "secret",
            "--ready-file",
            "ready",
            "--node-id",
            "node",
            "--session-epoch",
            "epoch",
        ]
    )
    assert args.lifetime_seconds is None
