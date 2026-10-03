"""Trusted, independently recoverable validation and draft-PR publication.

This module has no execution/scheduler callback: retrying publication cannot run
coding again. Callers provide controller-recorded identities, never model claims.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class PublicationError(RuntimeError):
    """A conflicting identity or failed trusted validation blocks publication."""


class PublicationPending(PublicationError):
    """An ambiguous external effect requires read-only reconciliation."""


@dataclass(frozen=True)
class PublicationIntent:
    job_id: str
    repository: str
    issue_number: int
    source_revision: str
    base_branch: str
    base_commit: str
    result_commit: str
    worker_id: str
    run_id: str
    profile_version: str
    validation_commands: tuple[tuple[str, ...], ...]
    validation_timeout_seconds: float = 300

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Expected an allowlisted GitHub owner/repository identity")
        for commit in (self.base_commit, self.result_commit):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                raise ValueError("Exact lowercase Git object identities are required")
        if (
            not all(
                (
                    self.job_id,
                    self.source_revision,
                    self.worker_id,
                    self.run_id,
                    self.profile_version,
                    self.base_branch,
                )
            )
            or self.issue_number <= 0
        ):
            raise ValueError("Publication provenance is incomplete")
        if self.base_branch.startswith("-") or any(
            c in self.base_branch for c in "\n\r"
        ):
            raise ValueError("Invalid base branch")
        if not self.validation_commands or any(
            not command or any(not arg or "\0" in arg for arg in command)
            for command in self.validation_commands
        ):
            raise ValueError("At least one configured validation command is required")
        if not 0 < self.validation_timeout_seconds <= 86400:
            raise ValueError("Validation timeout must be bounded")

    @property
    def branch(self) -> str:
        # Full digest avoids lossy slugs, and the same logical job has one branch
        # across execution attempts. A different result requires operator review.
        token = hashlib.sha256(f"{self.repository}\0{self.job_id}".encode()).hexdigest()
        return f"agentd/job-{token}"

    def payload(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CollectedCodingResult:
    """Filled by authenticated worker collection, not parsed from model output."""

    job_id: str
    repository: str
    base_commit: str
    result_commit: str
    worker_id: str
    run_id: str
    succeeded: bool

    def verify(self, intent: PublicationIntent) -> None:
        if not self.succeeded or any(
            getattr(self, key) != getattr(intent, key)
            for key in (
                "job_id",
                "repository",
                "base_commit",
                "result_commit",
                "worker_id",
                "run_id",
            )
        ):
            raise PublicationError("Collected result does not match controller intent")


class PublicationStore:
    """Side tables in the controller SQLite DB; execution state is untouched."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS draft_publications (
                job_id TEXT PRIMARY KEY, intent TEXT NOT NULL,
                stage TEXT NOT NULL, evidence TEXT, pr TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS publication_candidates (
                job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                generation INTEGER NOT NULL, intent TEXT NOT NULL,
                stage TEXT NOT NULL, evidence TEXT, pr TEXT,
                previous_intent TEXT, previous_pr TEXT,
                published_at TEXT,
                PRIMARY KEY (job_id, run_id), UNIQUE (job_id, generation)
            )""")
            if "published_at" not in {
                row[1]
                for row in db.execute("PRAGMA table_info(publication_candidates)")
            }:
                db.execute(
                    "ALTER TABLE publication_candidates ADD COLUMN published_at TEXT"
                )
            db.execute("""CREATE TABLE IF NOT EXISTS publication_update_grants (
                event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                from_run_id TEXT NOT NULL, consumed_by_run_id TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS coding_repair_outcomes (
                job_id TEXT NOT NULL, run_id TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY (job_id, run_id)
            )""")
            for row in db.execute(
                "SELECT job_id, intent, stage, evidence, pr FROM draft_publications"
            ).fetchall():
                db.execute(
                    "INSERT OR IGNORE INTO publication_candidates "
                    "(job_id, run_id, generation, intent, stage, evidence, pr, "
                    "previous_intent, previous_pr) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, NULL, NULL)",
                    (row[0], json.loads(row[1])["run_id"], *row[1:]),
                )
            db.execute(
                "UPDATE publication_candidates SET published_at = ? "
                "WHERE stage = 'published' AND published_at IS NULL",
                (datetime.now(UTC).isoformat(),),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Inspect durable publication progress without binding a new intent."""
        with self._connect() as db:
            row = db.execute(
                "SELECT stage, evidence, pr, intent FROM draft_publications "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            repair = db.execute(
                "SELECT status, reason FROM coding_repair_outcomes "
                "WHERE job_id = ? AND run_id = ?",
                (job_id, json.loads(row[3])["run_id"] if row else ""),
            ).fetchone()
            delivered = db.execute(
                "SELECT intent, pr, published_at FROM publication_candidates "
                "WHERE job_id = ? AND stage = 'published' "
                "ORDER BY generation DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "stage": row[0],
            "evidence": json.loads(row[1]) if row[1] else None,
            "pr": json.loads(row[2]) if row[2] else None,
            "intent": json.loads(row[3]),
            "repair": {"status": repair[0], "reason": repair[1]} if repair else None,
            "delivery": {
                "outcome": "delivered",
                "pr_url": json.loads(delivered[1])["url"],
                "result_commit": json.loads(delivered[0])["result_commit"],
                "run_id": json.loads(delivered[0])["run_id"],
                "acceptance": "pending-human-review",
                "completed_at": delivered[2],
            }
            if delivered
            else None,
        }

    def candidates(self, job_id: str) -> tuple[dict[str, Any], ...]:
        """Read all immutable candidate identities and retained validation."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT generation, intent, stage, evidence, pr "
                "FROM publication_candidates WHERE job_id = ? ORDER BY generation",
                (job_id,),
            ).fetchall()
        return tuple(
            {
                "generation": row[0],
                "intent": json.loads(row[1]),
                "stage": row[2],
                "evidence": json.loads(row[3]) if row[3] else None,
                "pr": json.loads(row[4]) if row[4] else None,
            }
            for row in rows
        )

    def authorize_update(self, job_id: str, run_id: str, *, event_id: str) -> None:
        """Record trusted GitHub feedback authority for one delivered result."""
        if not event_id or "\0" in event_id:
            raise PublicationError("Publication update requires a trusted event")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT intent, stage FROM draft_publications WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or row[1] != "published"
                or json.loads(row[0])["run_id"] != run_id
            ):
                raise PublicationError("Feedback does not target a delivered candidate")
            db.execute(
                "INSERT OR IGNORE INTO publication_update_grants "
                "VALUES (?, ?, ?, NULL)",
                (event_id, job_id, run_id),
            )
            grant = db.execute(
                "SELECT job_id, from_run_id FROM publication_update_grants "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if grant != (job_id, run_id):
                raise PublicationError("Feedback event identity changed")

    def record_repair(self, job_id: str, run_id: str, status: str, reason: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO coding_repair_outcomes VALUES (?, ?, ?, ?) "
                "ON CONFLICT(job_id, run_id) DO UPDATE SET "
                "status = excluded.status, reason = excluded.reason",
                (job_id, run_id, status, reason),
            )

    def repair_for(self, job_id: str, run_id: str) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status, reason FROM coding_repair_outcomes "
                "WHERE job_id = ? AND run_id = ?",
                (job_id, run_id),
            ).fetchone()
        return {"status": row[0], "reason": row[1]} if row else None

    @contextmanager
    def locked(self) -> Iterator[None]:
        # No long SQLite write transaction: quota/scheduler operations continue.
        # Kernel drops this lock on process death; all publisher instances share it.
        with Path(str(self.path) + ".publication.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def bind(self, intent: PublicationIntent) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO draft_publications "
                "VALUES (?, ?, 'pending', NULL, NULL)",
                (intent.job_id, intent.payload()),
            )
            row = db.execute(
                "SELECT intent, stage, evidence, pr FROM draft_publications "
                "WHERE job_id = ?",
                (intent.job_id,),
            ).fetchone()
            if row is None:
                raise PublicationError("Publication identity is unavailable")
            previous_intent = previous_pr = None
            if row[0] != intent.payload():
                old = json.loads(row[0])
                new = json.loads(intent.payload())
                stable = set(new) - {"run_id", "worker_id", "result_commit"}
                if old["run_id"] == intent.run_id or any(
                    old[k] != new[k] for k in stable
                ):
                    raise PublicationError(
                        "Logical job already bound to a different publication"
                    )
                if db.execute(
                    "SELECT 1 FROM publication_candidates "
                    "WHERE job_id = ? AND run_id = ?",
                    (intent.job_id, intent.run_id),
                ).fetchone():
                    raise PublicationError(
                        "A superseded candidate cannot be reselected"
                    )
                candidate = db.execute(
                    "SELECT previous_intent, previous_pr FROM publication_candidates "
                    "WHERE job_id = ? AND run_id = ?",
                    (intent.job_id, old["run_id"]),
                ).fetchone()
                if row[1] == "published":
                    grant = db.execute(
                        "SELECT event_id FROM publication_update_grants "
                        "WHERE job_id = ? "
                        "AND from_run_id = ? AND consumed_by_run_id IS NULL LIMIT 1",
                        (intent.job_id, old["run_id"]),
                    ).fetchone()
                    if grant is None:
                        raise PublicationError(
                            "Delivered candidate has no trusted update grant"
                        )
                    db.execute(
                        "UPDATE publication_update_grants SET consumed_by_run_id = ? "
                        "WHERE event_id = ?",
                        (intent.run_id, grant[0]),
                    )
                    previous_intent, previous_pr = row[0], row[3]
                elif row[1] == "validation_failed":
                    previous_intent, previous_pr = candidate or (None, None)
                else:
                    raise PublicationError(
                        "Publication external effects remain unresolved"
                    )
                db.execute(
                    "UPDATE draft_publications SET intent = ?, stage = 'pending', "
                    "evidence = NULL, pr = NULL WHERE job_id = ?",
                    (intent.payload(), intent.job_id),
                )
                row = (intent.payload(), "pending", None, None)
            generation = db.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM publication_candidates "
                "WHERE job_id = ?",
                (intent.job_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO publication_candidates "
                "(job_id, run_id, generation, intent, stage, evidence, pr, "
                "previous_intent, previous_pr) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.job_id,
                    intent.run_id,
                    generation,
                    *row,
                    previous_intent,
                    previous_pr,
                ),
            )
            prior = db.execute(
                "SELECT previous_intent, previous_pr FROM publication_candidates "
                "WHERE job_id = ? AND run_id = ?",
                (intent.job_id, intent.run_id),
            ).fetchone()
        return {
            "stage": row[1],
            "evidence": json.loads(row[2]) if row[2] else None,
            "pr": json.loads(row[3]) if row[3] else None,
            "previous_intent": json.loads(prior[0]) if prior[0] else None,
            "previous_pr": json.loads(prior[1]) if prior[1] else None,
        }

    def save(
        self,
        intent: PublicationIntent,
        stage: str,
        *,
        evidence: list[dict[str, Any]] | None = None,
        pr: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as db:
            prior = db.execute(
                "SELECT evidence, stage FROM publication_candidates "
                "WHERE job_id = ? AND run_id = ?",
                (intent.job_id, intent.run_id),
            ).fetchone()
            if evidence is not None and prior and prior[0] is not None:
                recorded = json.loads(prior[0])
                if evidence[: len(recorded)] != recorded:
                    raise PublicationError("Candidate validation evidence is immutable")
            if prior is not None and prior[1] == "published" and stage != "published":
                raise PublicationError("Delivered candidate state is terminal")
            cursor = db.execute(
                "UPDATE draft_publications SET stage = ?, "
                "evidence = COALESCE(?, evidence), pr = COALESCE(?, pr) "
                "WHERE job_id = ? AND intent = ?",
                (
                    stage,
                    json.dumps(evidence) if evidence is not None else None,
                    json.dumps(pr) if pr is not None else None,
                    intent.job_id,
                    intent.payload(),
                ),
            )
            if cursor.rowcount != 1:
                raise PublicationError("Publication identity changed")
            db.execute(
                "UPDATE publication_candidates SET stage = ?, "
                "evidence = COALESCE(?, evidence), pr = COALESCE(?, pr), "
                "published_at = CASE WHEN ? = 'published' "
                "THEN COALESCE(published_at, ?) ELSE published_at END "
                "WHERE job_id = ? AND run_id = ? AND intent = ?",
                (
                    stage,
                    json.dumps(evidence) if evidence is not None else None,
                    json.dumps(pr) if pr is not None else None,
                    stage,
                    datetime.now(UTC).isoformat(),
                    intent.job_id,
                    intent.run_id,
                    intent.payload(),
                ),
            )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git(repository: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = _run(
        (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository),
            *args,
        ),
        env=env,
    )
    if result.returncode:
        # Git diagnostics can contain credential-bearing remote URLs.
        raise PublicationError("Trusted Git operation failed")
    return result.stdout.strip()


def ensure_authorized_base(
    repository: Path,
    expected_repository: str,
    clone_url: str,
    base_commit: str,
) -> None:
    """Fetch the approved base from the controller-owned profile remote.

    The worker bundle is allowed to provide the result objects, but it cannot
    establish the authorized base.  The profile's immutable GitHub clone URL
    is the only source accepted for that boundary.
    """
    if clone_url != f"https://github.com/{expected_repository}.git":
        raise PublicationError("Profile clone URL does not match authorized repository")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit):
        raise PublicationError("Authorized base must be an exact Git object identity")
    try:
        _git(repository, "cat-file", "-e", f"{base_commit}^{{commit}}")
    except PublicationError:
        _git(
            repository,
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            clone_url,
            base_commit,
        )
        _git(repository, "cat-file", "-e", f"{base_commit}^{{commit}}")
    _git(
        repository,
        "update-ref",
        "refs/agentd/bases/" + base_commit,
        base_commit,
    )


def import_coding_bundle(
    intent: PublicationIntent,
    collected: CollectedCodingResult,
    evidence: dict[str, Any],
    repository: Path,
) -> str:
    """Import authenticated bounded worker evidence into a trusted object cache.

    The caller must first ensure the pinned base was fetched from the
    allowlisted repository. Neither worker paths nor worker
    remotes/configuration are consulted. Keep
    the returned controller ref until publication retention policy permits GC.
    """
    collected.verify(intent)
    for key in ("job_id", "repository", "base_commit", "result_commit", "run_id"):
        if evidence.get(key) != getattr(intent, key):
            raise PublicationError("Bundle provenance does not match controller intent")
    if evidence.get("source_revision") != intent.source_revision:
        raise PublicationError(
            "Bundle source revision does not match controller intent"
        )
    chunks = evidence.get("bundle_chunks")
    if (
        not isinstance(chunks, list)
        or not chunks
        or len(chunks) > 24
        or any(not isinstance(c, str) or len(c) > 16000 for c in chunks)
    ):
        raise PublicationError("Missing or oversized result bundle")
    try:
        bundle = base64.b64decode("".join(chunks), validate=True)
    except (ValueError, binascii.Error) as error:
        raise PublicationError("Malformed result bundle") from error
    if len(bundle) > 256 * 1024 or hashlib.sha256(bundle).hexdigest() != evidence.get(
        "bundle_sha256"
    ):
        raise PublicationError("Result bundle digest or size mismatch")
    with tempfile.TemporaryDirectory(prefix="agentd-result-") as temp:
        bundle_path = Path(temp) / "result.bundle"
        bundle_path.write_bytes(bundle)
        _git(repository, "bundle", "verify", str(bundle_path))
        heads = _git(repository, "bundle", "list-heads", str(bundle_path))
        # A worker cannot smuggle arbitrary ref updates into the controller.
        if not any(
            line.split()[0] == intent.result_commit for line in heads.splitlines()
        ):
            raise PublicationError("Bundle does not advertise the recorded result")
        _git(
            repository,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            str(bundle_path),
            intent.result_commit,
        )
    _git(
        repository,
        "merge-base",
        "--is-ancestor",
        intent.base_commit,
        intent.result_commit,
    )
    ref = "refs/agentd/results/" + hashlib.sha256(intent.run_id.encode()).hexdigest()
    existing = _run(("git", "-C", str(repository), "rev-parse", "--verify", ref))
    if existing.returncode == 0 and existing.stdout.strip() != intent.result_commit:
        raise PublicationError("Run result ref already identifies a different commit")
    _git(
        repository,
        "update-ref",
        ref,
        intent.result_commit,
        existing.stdout.strip()
        if existing.returncode == 0
        else "0" * len(intent.result_commit),
    )
    return ref


class ValidationRunner(Protocol):
    """Administrative adapter enforcing credential-free process containment."""

    def run(
        self, command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


class BubblewrapValidationRunner:
    """Linux validation sandbox with no host home, network or publisher state.

    Extra runtime mounts must contain only trusted toolchains/dependencies, never
    credentials or controller state. The result checkout is the only writable
    host mount; namespace contents and subprocesses disappear with the sandbox.
    """

    def __init__(
        self,
        *,
        executable: str = "/usr/bin/bwrap",
        runtime_mounts: tuple[Path, ...] = (),
    ) -> None:
        self.executable = executable
        self.runtime_mounts = runtime_mounts

    def run(
        self, command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            self.executable,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--dir",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for mount in (
            Path("/usr"),
            Path("/bin"),
            Path("/lib"),
            Path("/lib64"),
            *self.runtime_mounts,
        ):
            resolved = mount.resolve()
            if mount.exists():
                if str(resolved) in ("/", "/home", "/root", "/Users", "/etc"):
                    raise PublicationError("Validation runtime mount is too broad")
                arguments.extend(("--ro-bind", str(mount), str(mount)))
        arguments.extend(
            (
                "--bind",
                str(cwd),
                "/workspace",
                "--ro-bind-try",
                str(cwd / ".git"),
                "/workspace/.git",
                "--chdir",
                "/workspace",
                "--setenv",
                "LD_LIBRARY_PATH",
                "/usr/local/lib",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "PATH",
                env["PATH"],
                "--setenv",
                "GIT_CONFIG_NOSYSTEM",
                "1",
                "--setenv",
                "GIT_CONFIG_GLOBAL",
                "/dev/null",
                "--setenv",
                "GIT_TERMINAL_PROMPT",
                "0",
                "--setenv",
                "GIT_NO_REPLACE_OBJECTS",
                "1",
                "--",
                *command,
            )
        )
        return _run(arguments, timeout=timeout, env={"PATH": "/usr/bin:/bin"})


class MacOSSandboxValidationRunner:
    """Deny-default local validation using the operating-system sandbox.

    Runtime roots are explicit read-only administrative toolchain grants. Home,
    controller databases, network, keychain IPC and other process inspection are
    denied. Only the result checkout and a fresh scratch directory are writable.
    """

    def __init__(
        self,
        *,
        executable: str = "/usr/bin/sandbox-exec",
        runtime_mounts: tuple[Path, ...] = (),
    ) -> None:
        self.executable = executable
        self.runtime_mounts = runtime_mounts

    def run(
        self, command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        roots = (
            Path("/System/Library"),
            Path("/usr/lib"),
            Path("/usr/bin"),
            Path("/bin"),
            Path("/Library/Developer/CommandLineTools"),
            *self.runtime_mounts,
        )
        for root in roots:
            if str(root.resolve()) in ("/", "/Users", "/home", "/root", "/etc"):
                raise PublicationError("Validation runtime mount is too broad")
        with tempfile.TemporaryDirectory(prefix="agentd-validation-home-") as scratch:
            readonly = " ".join(
                f"(subpath {json.dumps(str(root.resolve()))})" for root in roots
            )
            writable = " ".join(
                f"(subpath {json.dumps(str(path.resolve()))})"
                for path in (cwd, Path(scratch))
            )
            git_metadata = json.dumps(str((cwd / ".git").resolve()))
            profile = (
                "(version 1)(deny default)"
                "(allow process-exec process-fork)"
                "(allow file-read-metadata)"
                '(allow file-read-data (literal "/"))'
                f"(allow file-read* {readonly} {writable} "
                '(literal "/dev/null") (literal "/dev/urandom") '
                '(literal "/private/etc/localtime"))'
                f'(allow file-write* {writable} (literal "/dev/null"))'
                f"(deny file-write* (subpath {git_metadata}))"
                '(allow sysctl-read (sysctl-name "hw.ncpu") '
                '(sysctl-name "hw.activecpu") (sysctl-name "hw.memsize") '
                '(sysctl-name "hw.pagesize") (sysctl-name "kern.osrelease") '
                '(sysctl-name "kern.ostype") (sysctl-name "kern.osversion"))'
            )
            clean_env = {
                "PATH": env["PATH"],
                "HOME": scratch,
                "TMPDIR": scratch,
                "XDG_CONFIG_HOME": scratch,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
            return _run(
                (self.executable, "-p", profile, *command),
                cwd=cwd,
                env=clean_env,
                timeout=timeout,
            )


class TrustedFinalizer:
    """Verify Git objects and collect actual process exit evidence.

    repository is a controller-owned bare object cache/import, never a worker
    checkout with worker-controlled Git config. Validation commands come only
    from the pinned administrative profile. Validation must run under an OS
    identity/container with no publisher credentials. A contained runner is
    mandatory; the default has no uncontained subprocess fallback.
    """

    def __init__(self, runner: ValidationRunner | None = None) -> None:
        self.runner = runner

    def validate(
        self,
        intent: PublicationIntent,
        collected: CollectedCodingResult,
        repository: Path,
    ) -> list[dict[str, Any]]:
        collected.verify(intent)
        if self.runner is None:
            raise PublicationError("A contained validation runner must be configured")
        with tempfile.TemporaryDirectory(prefix="agentd-validation-") as temp:
            root = Path(temp)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
            checkout = root / "result"
            clone = _run(
                (
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--",
                    str(repository.resolve()),
                    str(checkout),
                ),
                env=env,
            )
            if clone.returncode:
                raise PublicationError("Could not import collected Git objects")
            _git(
                checkout,
                "fetch",
                "--no-tags",
                str(repository.resolve()),
                intent.result_commit,
                env=env,
            )
            for commit in (intent.base_commit, intent.result_commit):
                if (
                    _git(checkout, "rev-parse", f"{commit}^{{commit}}", env=env)
                    != commit
                ):
                    raise PublicationError(
                        "Result does not identify an exact Git commit"
                    )
            _git(
                checkout,
                "merge-base",
                "--is-ancestor",
                intent.base_commit,
                intent.result_commit,
                env=env,
            )
            _git(checkout, "checkout", "--detach", intent.result_commit, env=env)
            # Never execute trusted Git against metadata writable by validation.
            # A forged core.fsmonitor hook would otherwise escape containment.
            trusted_git = root / "trusted.git"
            shutil.copytree(checkout / ".git", trusted_git)
            evidence = []
            for command in intent.validation_commands:
                try:
                    result = self.runner.run(
                        command,
                        cwd=checkout,
                        env=env,
                        timeout=intent.validation_timeout_seconds,
                    )
                    evidence.append(
                        {
                            "command": list(command),
                            "commit": intent.result_commit,
                            "returncode": result.returncode,
                            "stdout_sha256": hashlib.sha256(
                                result.stdout.encode()
                            ).hexdigest(),
                            "stderr_sha256": hashlib.sha256(
                                result.stderr.encode()
                            ).hexdigest(),
                            "stdout": result.stdout[-4096:].replace("\0", ""),
                            "stderr": result.stderr[-4096:].replace("\0", ""),
                        }
                    )
                except (OSError, subprocess.TimeoutExpired):
                    evidence.append(
                        {
                            "command": list(command),
                            "commit": intent.result_commit,
                            "returncode": None,
                            "error": "unavailable-or-timeout",
                        }
                    )
                if evidence[-1]["returncode"] != 0:
                    return evidence
            # Inspect content using metadata outside the sandbox's writable
            # grants. Do not consult validation-controlled config, hooks, index,
            # replacement refs or nested submodule repositories after execution.
            try:
                _git(
                    checkout,
                    "--git-dir",
                    str(trusted_git),
                    "--work-tree",
                    str(checkout),
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=all",
                    "--quiet",
                    intent.result_commit,
                    "--",
                    env=env,
                )
            except PublicationError:
                evidence.append(
                    {
                        "commit": intent.result_commit,
                        "returncode": None,
                        "error": "validation-mutated-result",
                    }
                )
            return evidence


class PublicationAdapter(Protocol):
    def branch_commit(self, intent: PublicationIntent) -> str | None: ...
    def push(self, intent: PublicationIntent, repository: Path) -> None: ...
    def find_pr(self, intent: PublicationIntent) -> dict[str, Any] | None: ...
    def create_draft(self, intent: PublicationIntent, body: str) -> dict[str, Any]: ...
    def update_branch(
        self, intent: PublicationIntent, repository: Path, expected_commit: str
    ) -> None: ...
    def update_pr(
        self, intent: PublicationIntent, pr: dict[str, Any], body: str
    ) -> dict[str, Any]: ...


class DraftPublisher:
    def __init__(
        self,
        store: PublicationStore,
        adapter: PublicationAdapter,
        finalizer: TrustedFinalizer | None = None,
        *,
        maximum_validation_attempts: int = 3,
        allow_ready_pr_updates: bool = False,
    ) -> None:
        self.store, self.adapter = store, adapter
        self.finalizer = finalizer or TrustedFinalizer()
        if not 1 <= maximum_validation_attempts <= 10:
            raise ValueError("Trusted validation retries must be bounded")
        self.maximum_validation_attempts = maximum_validation_attempts
        self.allow_ready_pr_updates = allow_ready_pr_updates

    def publish(
        self,
        intent: PublicationIntent,
        collected: CollectedCodingResult,
        repository: Path,
        *,
        authorization_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        collected.verify(intent)
        with self.store.locked():
            state = self.store.bind(intent)
            if state["stage"] == "published":
                return state["pr"]
            if state["stage"] == "validation_failed":
                raise PublicationError("Recorded trusted validation failed")
            if state["stage"] == "validation_blocked":
                raise PublicationError("Recorded trusted validation boundary failed")
            if state["evidence"] is None or state["stage"] == "validation_unavailable":
                recorded = state["evidence"] or []
                attempt = (
                    max(
                        (item.get("validation_attempt", 1) for item in recorded),
                        default=0,
                    )
                    + 1
                )
                if attempt > self.maximum_validation_attempts:
                    raise PublicationPending(
                        "Trusted validation infrastructure retry limit reached"
                    )
                try:
                    evidence = self.finalizer.validate(intent, collected, repository)
                except (OSError, subprocess.TimeoutExpired):
                    evidence = [
                        {
                            "commit": intent.result_commit,
                            "returncode": None,
                            "error": "unavailable-or-timeout",
                        }
                    ]
                except PublicationError:
                    self.store.save(
                        intent,
                        "validation_blocked",
                        evidence=[
                            *recorded,
                            {
                                "commit": intent.result_commit,
                                "returncode": None,
                                "error": "trusted-validation-boundary",
                                "validation_attempt": attempt,
                            },
                        ],
                    )
                    raise
                valid = bool(evidence) and all(
                    item.get("returncode") == 0 for item in evidence
                )
                unavailable = any(
                    item.get("error") == "unavailable-or-timeout" for item in evidence
                )
                evidence = [
                    {**item, "validation_attempt": attempt} for item in evidence
                ]
                self.store.save(
                    intent,
                    "validated"
                    if valid
                    else "validation_unavailable"
                    if unavailable
                    else "validation_failed",
                    evidence=recorded + evidence,
                )
                if not valid:
                    if unavailable:
                        raise PublicationPending(
                            "Trusted validation infrastructure unavailable"
                        )
                    raise PublicationError("Trusted validation failed")
            previous = state["previous_intent"]
            if previous is not None:
                _git(
                    repository,
                    "merge-base",
                    "--is-ancestor",
                    previous["result_commit"],
                    intent.result_commit,
                )
                existing = self.adapter.find_pr(intent)
                if (
                    existing is None
                    or existing.get("url") != state["previous_pr"]["url"]
                    or existing.get("state", "OPEN") != "OPEN"
                    or existing.get("headRefOid")
                    not in {
                        previous["result_commit"],
                        intent.result_commit,
                    }
                    or (not self.allow_ready_pr_updates and not existing.get("isDraft"))
                    or existing.get("headRefName") != intent.branch
                    or existing.get("baseRefName") != intent.base_branch
                    or existing.get("isCrossRepository") is not False
                    or f"<!-- agentd-publication:{intent.branch} -->"
                    not in existing.get("body", "")
                ):
                    raise PublicationError("Previously delivered PR cannot be updated")
            if authorization_check is not None:
                authorization_check()
            head = self.adapter.branch_commit(intent)
            if head is not None and head != intent.result_commit:
                if previous is None or head != previous["result_commit"]:
                    raise PublicationError(
                        "Publication branch points to a conflicting result"
                    )
                if authorization_check is not None:
                    authorization_check()
                self.store.save(intent, "push_requested")
                self.adapter.update_branch(
                    intent, repository, previous["result_commit"]
                )
                if self.adapter.branch_commit(intent) != intent.result_commit:
                    raise PublicationPending("Branch update needs reconciliation")
            if head is None:
                if previous is not None:
                    raise PublicationError("Previously delivered branch is missing")
                if state["stage"] == "create_requested":
                    raise PublicationPending(
                        "PR creation and branch ownership unresolved"
                    )
                if authorization_check is not None:
                    authorization_check()
                self.store.save(intent, "push_requested")
                self.adapter.push(intent, repository)
                if self.adapter.branch_commit(intent) != intent.result_commit:
                    raise PublicationPending("Branch push needs reconciliation")
            pr = self.adapter.find_pr(intent)
            if previous is not None:
                if pr is None or pr.get("url") != state["previous_pr"]["url"]:
                    raise PublicationError("Previously delivered PR is unresolved")
                if authorization_check is not None:
                    authorization_check()
                self.store.save(intent, "pr_update_requested")
                pr = self.adapter.update_pr(intent, pr, self._body(intent))
            if pr is None:
                # Once a create could have reached GitHub, absence in a read is
                # not proof it failed. Never blindly replay a non-idempotent POST.
                if state["stage"] == "create_requested":
                    raise PublicationPending("PR creation ownership remains unresolved")
                if authorization_check is not None:
                    authorization_check()
                self.store.save(intent, "create_requested")
                pr = self.adapter.create_draft(intent, self._body(intent))
            self._verify_pr(
                intent, pr, allow_ready=bool(previous) and self.allow_ready_pr_updates
            )
            self.store.save(intent, "published", pr=pr)
            return pr

    @staticmethod
    def _body(intent: PublicationIntent) -> str:
        provenance = {
            key: value
            for key, value in asdict(intent).items()
            if key not in ("validation_commands", "validation_timeout_seconds")
        }
        return (
            f"Resolves #{intent.issue_number}\n\n"
            "agentd generated this draft for human/CI review. Validation: passed.\n\n"
            f"```json\n{json.dumps(provenance, indent=2, sort_keys=True)}\n```\n"
            f"<!-- agentd-publication:{intent.branch} -->"
        )

    @staticmethod
    def _verify_pr(
        intent: PublicationIntent, pr: dict[str, Any], *, allow_ready: bool = False
    ) -> None:
        if (
            pr.get("headRefName") != intent.branch
            or pr.get("headRefOid") != intent.result_commit
            or pr.get("baseRefName") != intent.base_branch
            or pr.get("isCrossRepository") is not False
            or (not allow_ready and not pr.get("isDraft"))
            or pr.get("state", "OPEN") != "OPEN"
            or not pr.get("url")
            or f"<!-- agentd-publication:{intent.branch} -->" not in pr.get("body", "")
        ):
            raise PublicationError("Existing PR does not match intended draft result")


class GitHubPublicationAdapter:
    """Small privileged adapter. No merge operation exists on this interface."""

    def __init__(self, *, timeout_seconds: float = 60) -> None:
        self.timeout_seconds = timeout_seconds

    def _gh(self, *args: str) -> Any:
        result = _run(("gh", *args), timeout=self.timeout_seconds)
        if result.returncode:
            raise PublicationPending("GitHub operation failed; reconcile before retry")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def branch_commit(self, intent: PublicationIntent) -> str | None:
        # GraphQL returns a null ref for absence; authentication/network failures
        # must never masquerade as a missing branch.
        owner, name = intent.repository.split("/")
        data = self._gh(
            "api",
            "graphql",
            "-f",
            "query=query($owner:String!,$name:String!,"
            "$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref)"
            "{target{oid}}}}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"ref=refs/heads/{intent.branch}",
        )
        repository = data.get("data", {}).get("repository")
        if not repository or data.get("errors"):
            raise PublicationPending("Repository lookup failed")
        ref = repository["ref"]
        return ref["target"]["oid"] if ref else None

    def push(self, intent: PublicationIntent, repository: Path) -> None:
        # An empty expected ref means create-only; a race cannot overwrite a
        # human edit or another result, even when the first ACK was lost.
        _git(
            repository,
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "push",
            f"--force-with-lease=refs/heads/{intent.branch}:",
            f"https://github.com/{intent.repository}.git",
            f"{intent.result_commit}:refs/heads/{intent.branch}",
        )

    def update_branch(
        self, intent: PublicationIntent, repository: Path, expected_commit: str
    ) -> None:
        _git(
            repository,
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "push",
            f"--force-with-lease=refs/heads/{intent.branch}:{expected_commit}",
            f"https://github.com/{intent.repository}.git",
            f"{intent.result_commit}:refs/heads/{intent.branch}",
        )

    def update_pr(
        self, intent: PublicationIntent, pr: dict[str, Any], body: str
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="agentd-pr-update-") as temp:
            body_path = Path(temp) / "body.md"
            body_path.write_text(body)
            result = _run(
                (
                    "gh",
                    "pr",
                    "edit",
                    pr["url"],
                    "--repo",
                    intent.repository,
                    "--body-file",
                    str(body_path),
                ),
                timeout=self.timeout_seconds,
            )
            if result.returncode:
                raise PublicationPending("PR update requires reconciliation")
        updated = self.find_pr(intent)
        if updated is None:
            raise PublicationPending("Updated PR is not visible yet")
        return updated

    def find_pr(self, intent: PublicationIntent) -> dict[str, Any] | None:
        prs = self._gh(
            "pr",
            "list",
            "--repo",
            intent.repository,
            "--state",
            "all",
            "--head",
            intent.branch,
            "--limit",
            "100",
            "--json",
            "url,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,body,state",
        )
        if len(prs) > 1:
            raise PublicationError("Multiple PRs claim this logical job")
        return prs[0] if prs else None

    def create_draft(self, intent: PublicationIntent, body: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="agentd-pr-") as temp:
            body_path = Path(temp) / "body.md"
            body_path.write_text(body)
            result = _run(
                (
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    intent.repository,
                    "--draft",
                    "--head",
                    intent.branch,
                    "--base",
                    intent.base_branch,
                    "--title",
                    f"agentd: address issue #{intent.issue_number}",
                    "--body-file",
                    str(body_path),
                ),
                timeout=self.timeout_seconds,
            )
        if result.returncode:
            raise PublicationPending("PR creation requires reconciliation")
        pr = self.find_pr(intent)
        if pr is None:
            raise PublicationPending("Created PR is not visible yet")
        return pr
