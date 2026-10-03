"""Validate the exact agentd checkout using an immutable prepared toolchain.

Invoke this trusted file outside the candidate checkout, inside the publication
sandbox. The script itself is not a sandbox. Its Python dependency runtime must
be read-only and contain the complete locked project dependencies plus pytest,
ruff, build, hatchling, and pip >= 22.3. It never installs from a network index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import BytesParser
from math import isfinite
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PYTEST = (
    "import pathlib,sys,pytest; "
    "sys.path.insert(0,sys.argv[1]); import agentd; "
    "assert pathlib.Path(agentd.__file__).resolve().is_relative_to("
    "pathlib.Path(sys.argv[1]).resolve()), 'tests imported an installed old agentd'; "
    "sys.exit(pytest.main(sys.argv[2:]))"
)
_DOCS = (
    "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
    "sys.argv=[sys.argv[2]]; runpy.run_path(sys.argv[0],run_name='__main__')"
)
_SMOKE = (
    "import importlib.metadata,json,pathlib,runpy,sys,sysconfig; "
    "sys.path.extend(json.loads(sys.argv[1])); import agentd; "
    "root=pathlib.Path(sysconfig.get_path('purelib')).resolve(); "
    "assert pathlib.Path(agentd.__file__).resolve().is_relative_to(root), "
    "'smoke imported source or old installed agentd'; "
    "dist=importlib.metadata.distribution('agentd'); "
    "assert pathlib.Path(dist.locate_file('')).resolve()==root; "
    "assert dist.version==sys.argv[2]; "
    "from packaging.requirements import Requirement; "
    "requirements=[Requirement(value) for value in dist.requires or ()]; "
    "assert all(req.specifier.contains(importlib.metadata.version(req.name)) "
    "for req in requirements if req.marker is None or req.marker.evaluate()), "
    "'prepared dependency versions do not satisfy the wheel'; "
    "entry=[e for e in dist.entry_points if e.group=='console_scripts' "
    "and e.name=='agentd']; assert len(entry)==1; "
    "cli=pathlib.Path(sys.prefix)/'bin'/'agentd'; assert cli.is_file(); "
    "sys.argv=[str(cli),'--help']; runpy.run_path(str(cli),run_name='__main__')"
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    seconds: float = 0


Runner = Callable[[tuple[str, ...], Path, dict[str, str], float], CommandResult]


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_command(
    argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout: float
) -> CommandResult:
    started = time.monotonic()
    # Files keep hostile test output out of controller memory. /tmp is a private
    # bounded sandbox mount; only the tail and complete digest enter evidence.
    with tempfile.TemporaryDirectory(dir=env["TMPDIR"], prefix="command-") as temp:
        stdout_path, stderr_path = Path(temp) / "stdout", Path(temp) / "stderr"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout,
                    check=False,
                )
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                returncode = 124
        tails = []
        for path in (stdout_path, stderr_path):
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 2048))
                tails.append(stream.read().decode("utf-8", errors="replace"))
        return CommandResult(
            returncode,
            *tails,
            sha256_file(stdout_path),
            sha256_file(stderr_path),
            round(time.monotonic() - started, 3),
        )


def project_metadata(checkout: Path) -> dict[str, Any]:
    project = tomllib.loads((checkout / "pyproject.toml").read_text())["project"]
    if project.get("name") != "agentd" or not isinstance(project.get("version"), str):
        raise ValueError("Package validation requires static agentd name and version")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) for value in dependencies
    ):
        raise ValueError("Package validation requires declared dependency strings")
    return {
        "name": "agentd",
        "version": project["version"],
        "dependencies": dependencies,
    }


def canonical_requirement(value: str) -> tuple[str, str, tuple[str, ...], str]:
    requirement = Requirement(value)
    return (
        canonicalize_name(requirement.name),
        str(requirement.specifier),
        tuple(sorted(requirement.extras)),
        str(requirement.marker) if requirement.marker is not None else "",
    )


def wheel_metadata(
    wheel: Path, expected: dict[str, Any], *, checkout: Path | None = None
) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ValueError("Wheel must contain one distribution metadata record")
        info = archive.getinfo(metadata_files[0])
        if info.file_size > 1_048_576:
            raise ValueError("Wheel metadata exceeds bounded validation size")
        metadata = BytesParser().parsebytes(archive.read(info))
        if checkout is not None:
            source = checkout / "src"
            package = source / "agentd"
            files = sorted(package.rglob("*.py"))
            if not files:
                raise ValueError("Agentd checkout contains no Python package sources")
            for path in files:
                name = path.relative_to(source).as_posix()
                if path.is_symlink() or name not in archive.namelist():
                    raise ValueError("Wheel omits a checked Python source")
                if archive.read(name) != path.read_bytes():
                    raise ValueError(
                        "Wheel content differs from the checked Python source"
                    )
    found = {"name": metadata["Name"], "version": metadata["Version"]}
    if found != {key: expected[key] for key in ("name", "version")}:
        raise ValueError("Built wheel metadata differs from the checked source")
    required = {
        canonical_requirement(value)
        for value in metadata.get_all("Requires-Dist", [])
        if "extra" not in str(Requirement(value).marker)
    }
    if required != {
        canonical_requirement(value) for value in expected.get("dependencies", ())
    }:
        raise ValueError("Built wheel dependencies differ from the checked source")
    return expected


def validation_commands(
    checkout: Path, python: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    source = str(checkout / "src")
    return (
        ("git_diff", ("/usr/bin/git", "diff", "--check")),
        ("git_clean", ("/usr/bin/git", "diff", "--quiet", "HEAD", "--")),
        ("ruff", (python, "-I", "-m", "ruff", "check", ".")),
        ("format", (python, "-I", "-m", "ruff", "format", "--check", ".")),
        (
            "docs",
            (python, "-I", "-c", _DOCS, source, str(checkout / "tools/check_docs.py")),
        ),
        ("pytest", (python, "-I", "-c", _PYTEST, source, "-q")),
    )


def validate(
    checkout: Path,
    *,
    python: str = sys.executable,
    expected_commit: str | None = None,
    timeout: float = 600,
    runner: Runner = run_command,
) -> dict[str, Any]:
    checkout = checkout.resolve()
    if not Path(python).is_absolute() or not isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "Package validation requires absolute Python and positive timeout"
        )
    if expected_commit is not None and _COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("Expected package commit must be a complete lowercase Git SHA")
    metadata = project_metadata(checkout)
    result: dict[str, Any] = {"passed": False, "project": metadata, "steps": []}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agentd-package-validation-") as temp:
        scratch = Path(temp)
        env = {
            "PATH": str(Path(python).parent) + ":/usr/bin:/bin",
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "PYTHONPATH": str(checkout / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }

        def execute(
            name: str, argv: tuple[str, ...], cwd: Path = checkout
        ) -> CommandResult:
            remaining = timeout - (time.monotonic() - started)
            evidence = (
                runner(argv, cwd, env, remaining)
                if remaining > 0
                else CommandResult(124, stderr="Package validation deadline exhausted")
            )
            step = {
                "name": name,
                "returncode": evidence.returncode,
                "stdout_sha256": evidence.stdout_sha256,
                "stderr_sha256": evidence.stderr_sha256,
                "seconds": evidence.seconds,
            }
            if evidence.returncode:
                step["stdout_tail"] = evidence.stdout
                step["stderr_tail"] = evidence.stderr
            result["steps"].append(step)
            return evidence

        head = execute("commit", ("/usr/bin/git", "rev-parse", "HEAD"))
        commit = head.stdout.strip()
        if head.returncode or _COMMIT.fullmatch(commit) is None:
            return result
        result["commit"] = commit
        if expected_commit is not None and expected_commit != commit:
            result["error"] = "checkout_commit_mismatch"
            return result
        for name, argv in validation_commands(checkout, python):
            if execute(name, argv).returncode:
                return result
        artifacts = scratch / "artifacts"
        artifacts.mkdir()
        if execute(
            "build",
            (
                python,
                "-I",
                "-m",
                "build",
                "--no-isolation",
                "--sdist",
                "--wheel",
                "--outdir",
                str(artifacts),
            ),
        ).returncode:
            return result
        wheels, sdists = list(artifacts.glob("*.whl")), list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            result["error"] = "expected_one_wheel_and_sdist"
            return result
        if any(path.is_symlink() for path in (*wheels, *sdists)):
            raise ValueError("Package build artifacts must be regular files")
        wheel_metadata(wheels[0], metadata, checkout=checkout)
        runtime = execute(
            "runtime",
            (
                python,
                "-I",
                "-c",
                "import json,sysconfig; print(json.dumps(list(dict.fromkeys(["
                "sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]))))",
            ),
            scratch,
        )
        if runtime.returncode:
            return result
        dependency_paths = json.loads(runtime.stdout)
        if not isinstance(dependency_paths, list) or any(
            not isinstance(path, str) or not Path(path).is_absolute()
            for path in dependency_paths
        ):
            raise ValueError(
                "Prepared dependency runtime returned invalid library paths"
            )
        fresh = scratch / "fresh"
        if execute(
            "venv", (python, "-I", "-m", "venv", "--without-pip", str(fresh)), scratch
        ).returncode:
            return result
        fresh_python = str(fresh / "bin/python")
        if execute(
            "install",
            (
                python,
                "-I",
                "-m",
                "pip",
                "--python",
                fresh_python,
                "install",
                "--no-index",
                "--no-deps",
                "--no-compile",
                "--disable-pip-version-check",
                str(wheels[0]),
            ),
            scratch,
        ).returncode:
            return result
        if execute(
            "wheel_smoke",
            (
                fresh_python,
                "-I",
                "-c",
                _SMOKE,
                json.dumps(dependency_paths),
                metadata["version"],
            ),
            scratch,
        ).returncode:
            return result
        result["artifacts"] = [
            {
                "name": path.name,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in (wheels[0], sdists[0])
        ]
        result["passed"] = True
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--expected-commit")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    try:
        result = validate(
            args.checkout,
            python=args.python,
            expected_commit=args.expected_commit,
            timeout=args.timeout,
        )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        # Only sanitized error classes, never environment or credential values.
        result = {"passed": False, "error": type(error).__name__}
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
