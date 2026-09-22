#!/usr/bin/env python3
"""Run the persistent production coding worker.

This entrypoint intentionally omits the qualification tool's finite lifetime.
The supervisor owns restart and shutdown; a coding run remains live until the
worker receives SIGINT or SIGTERM.
"""

from __future__ import annotations

import asyncio
import sys

from qualify_coding_worker import parser, run


def main() -> None:
    asyncio.run(run(parser(include_lifetime=False).parse_args()))


if __name__ == "__main__":
    sys.exit(main())
