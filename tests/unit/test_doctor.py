from __future__ import annotations

import sqlite3
from pathlib import Path

from agentd.config import ServiceConfig
from agentd.doctor import _sqlite_check


def test_sqlite_check_accepts_absent_database(tmp_path: Path) -> None:
    check = _sqlite_check(tmp_path / "state.sqlite")

    assert check.ok
    assert check.detail == "database will be initialized"


def test_sqlite_check_reads_existing_database_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
    before = database.stat().st_mtime_ns

    check = _sqlite_check(database)

    assert check.ok
    assert check.detail == "ok"
    assert database.stat().st_mtime_ns == before


def test_service_config_paths_can_be_constructed_for_doctor(tmp_path: Path) -> None:
    config = ServiceConfig(
        database=tmp_path / "state" / "state.sqlite",
        workspace_root=tmp_path / "workspaces",
        codex_home=tmp_path / "codex-home",
        uv_cache=tmp_path / "uv-cache",
    )

    assert config.database.parent.name == "state"
