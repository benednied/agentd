"""Process lifecycle primitives shared by the controller and service wrappers.

The controller is intentionally single writer.  SQLite serializes individual
transactions, but it cannot prevent two schedulers from both dispatching the
same durable work between transactions.  ``ControllerLock`` supplies that
process-level ownership boundary and ``Readiness`` gives supervisors a stable
file based health contract without exposing a network endpoint.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


class ControllerAlreadyRunning(RuntimeError):
    """Raised when another controller owns the configured lock."""


class ControllerLock:
    """Exclusive advisory lock for one controller database.

    The lock file sits beside the database and is deliberately retained after
    exit; the kernel releases the ownership when the process dies.
    """

    def __init__(self, database: Path) -> None:
        self.path = database.with_name(f".{database.name}.controller.lock")
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise ControllerAlreadyRunning(
                f"controller lock is already held: {self.path}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def held(self) -> bool:
        """Probe kernel ownership; a retained PID file alone is not liveness."""
        if not self.path.exists():
            return False
        with self.path.open("r") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass(slots=True)
class Readiness:
    """Atomic readiness marker suitable for systemd and shell probes."""

    path: Path

    def mark_ready(self, *, pid: int | None = None) -> None:
        self._write("ready", pid)

    def mark_starting(self, *, pid: int | None = None) -> None:
        self._write("starting", pid)

    def mark_stopping(self, *, pid: int | None = None) -> None:
        self._write("stopping", pid)

    def clear(self) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()

    def is_ready(self) -> bool:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()[0] == "ready"
        except (FileNotFoundError, IndexError, UnicodeError):
            return False

    def _write(self, state: str, pid: int | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = f"{state}\n{os.getpid() if pid is None else pid}\n"
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
