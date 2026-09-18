"""Bounded credential/network/filesystem probe for the configured Linux runner.

Run under the actual validation deployment identity, with agentd importable.
Additional --runtime-mount directories must be credential-free toolchains only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from agentd.publication import BubblewrapValidationRunner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-mount", action="append", type=Path, default=[])
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agentd-publication-probe-") as temp:
        root = Path(temp)
        checkout = root / "checkout"
        checkout.mkdir()
        secret = root / "outside-secret"
        secret.write_text("synthetic-probe-secret")
        code = (
            "import os,pathlib,socket; "
            "assert 'GH_TOKEN' not in os.environ; "
            f"assert not pathlib.Path({str(secret)!r}).exists(); "
            "assert not pathlib.Path('/proc/1/environ').exists(); "
            "s=socket.socket(); s.settimeout(2); "
            "assert s.connect_ex(('192.0.2.1',443)) != 0; "
            "pathlib.Path('verified').write_text('contained'); "
            "print('containment passed')"
        )
        result = BubblewrapValidationRunner(
            runtime_mounts=tuple(args.runtime_mount)
        ).run(
            (args.python, "-c", code),
            cwd=checkout,
            env={"PATH": "/usr/bin:/bin", "GH_TOKEN": "synthetic-probe-token"},
            timeout=20,
        )
        passed = (
            result.returncode == 0
            and (checkout / "verified").is_file()
            and (checkout / "verified").read_text() == "contained"
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        )
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
