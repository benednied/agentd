from __future__ import annotations

from pathlib import Path

import pytest

from agentd.lifecycle import ControllerAlreadyRunning, ControllerLock, Readiness


def test_controller_lock_is_single_owner(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    first = ControllerLock(database)
    second = ControllerLock(database)
    first.acquire()
    try:
        with pytest.raises(ControllerAlreadyRunning):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_readiness_marker_is_atomic_and_stateful(tmp_path: Path) -> None:
    marker = Readiness(tmp_path / "run" / "ready")
    assert not marker.is_ready()
    marker.mark_starting(pid=42)
    assert not marker.is_ready()
    marker.mark_ready(pid=42)
    assert marker.is_ready()
    assert marker.path.read_text(encoding="utf-8") == "ready\n42\n"
    marker.mark_stopping(pid=42)
    assert not marker.is_ready()
    marker.clear()
    assert not marker.path.exists()
