#!/usr/bin/env python3
"""Create and validate SQLite snapshots using Python's bundled SQLite runtime."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def verify(path: Path) -> None:
    """Fail unless *path* is a readable SQLite database with valid pages."""

    with _read_only(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity validation failed: {result!r}")


def backup(source: Path, destination: Path) -> None:
    """Copy a consistent online snapshot from *source* to *destination*."""

    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        _read_only(source) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)
    verify(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backup", "verify"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()

    if args.command == "verify":
        if args.destination is not None:
            parser.error("verify accepts only SOURCE")
        verify(args.source)
    else:
        if args.destination is None:
            parser.error("backup requires DESTINATION")
        backup(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
