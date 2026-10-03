import json
import subprocess
import sys
import sysconfig
import venv
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from validate_python_package import (
    _SMOKE,
    CommandResult,
    project_metadata,
    validate,
    validation_commands,
    wheel_metadata,
)


def checkout(path):
    (path / "src/agentd").mkdir(parents=True)
    (path / "src/agentd/__init__.py").write_text("VALUE = 'candidate'\n")
    (path / "pyproject.toml").write_text('[project]\nname="agentd"\nversion="0.1.0"\n')
    return path


def wheel(path, source="VALUE = 'candidate'\n", name="agentd"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "agentd-0.1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n",
        )
        archive.writestr("agentd/__init__.py", source)


def test_package_metadata_and_wheel_must_match_exact_source(tmp_path):
    root = checkout(tmp_path / "checkout")
    built = tmp_path / "agentd.whl"
    metadata = project_metadata(root)
    wheel(built)
    assert wheel_metadata(built, metadata, checkout=root) == metadata
    wheel(built, source="VALUE = 'old release'\n")
    with pytest.raises(ValueError, match="differs"):
        wheel_metadata(built, metadata, checkout=root)
    wheel(built, name="different")
    with pytest.raises(ValueError, match="metadata differs"):
        wheel_metadata(built, metadata)


def test_fixed_commands_use_prepared_modules_and_candidate_source(tmp_path):
    root = checkout(tmp_path / "checkout")
    commands = dict(validation_commands(root, "/opt/validation/bin/python"))
    assert commands["ruff"][1:4] == ("-I", "-m", "ruff")
    assert commands["format"][-3:] == ("format", "--check", ".")
    assert commands["pytest"][1:3] == ("-I", "-c")
    assert str(root / "src") in commands["pytest"]
    assert "import pathlib,sys,pytest" in commands["pytest"][3]
    assert str(root / "tools/check_docs.py") in commands["docs"]
    assert all("uv" not in argv for argv in commands.values())


def test_orchestration_is_offline_and_does_not_inherit_credentials(
    tmp_path, monkeypatch
):
    root = checkout(tmp_path / "checkout")
    calls = []
    monkeypatch.setenv("GH_TOKEN", "synthetic secret")

    def runner(argv, cwd, env, timeout):
        calls.append((argv, cwd, env, timeout))
        if argv[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40 + "\n")
        if "build" in argv:
            artifacts = Path(argv[-1])
            wheel(artifacts / "agentd-0.1.0-py3-none-any.whl")
            (artifacts / "agentd-0.1.0.tar.gz").write_bytes(b"test sdist")
        if "import json,sysconfig" in argv[-1]:
            return CommandResult(0, json.dumps(["/opt/validation/site-packages"]))
        return CommandResult(0)

    result = validate(
        root,
        python="/opt/validation/bin/python",
        expected_commit="a" * 40,
        runner=runner,
    )
    assert result["passed"]
    assert len(result["artifacts"]) == 2
    assert all("GH_TOKEN" not in env for _argv, _cwd, env, _timeout in calls)
    assert all(env["PYTHONPATH"] == str(root / "src") for _, _, env, _ in calls)
    assert all(env["PIP_NO_INDEX"] == "1" for _, _, env, _ in calls)
    build = next(argv for argv, *_ in calls if "build" in argv)
    assert "--no-isolation" in build and "--sdist" in build and "--wheel" in build
    install = next(argv for argv, *_ in calls if "pip" in argv)
    assert "--no-index" in install and "--no-deps" in install and "--python" in install
    assert calls[-2][0][1:4] == ("-I", "-c", _SMOKE)
    assert calls[-1][0] == ("/usr/bin/git", "diff", "--quiet", "HEAD", "--")


def test_post_smoke_source_mutation_cannot_qualify_package(tmp_path):
    root = checkout(tmp_path / "checkout")
    diff_calls = []
    smoke_ran = []

    def runner(argv, cwd, env, timeout):
        if argv[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40)
        if argv == ("/usr/bin/git", "diff", "--quiet", "HEAD", "--"):
            diff_calls.append(argv)
            return CommandResult(1 if smoke_ran else 0)
        if "build" in argv:
            artifacts = Path(argv[-1])
            wheel(artifacts / "agentd-0.1.0-py3-none-any.whl")
            (artifacts / "agentd-0.1.0.tar.gz").write_bytes(b"test sdist")
        if "import json,sysconfig" in argv[-1]:
            return CommandResult(0, json.dumps(["/opt/validation/site-packages"]))
        if _SMOKE in argv:
            smoke_ran.append(True)
        return CommandResult(0)

    result = validate(root, runner=runner)
    assert smoke_ran and len(diff_calls) == 2
    assert not result["passed"]
    assert result["steps"][-1]["name"] == "final_diff"
    assert result["steps"][-1]["returncode"] == 1
    assert "artifacts" not in result


def test_commit_mismatch_and_failed_validation_stop_before_build(tmp_path):
    root = checkout(tmp_path / "checkout")
    calls = []

    def runner(argv, *_):
        calls.append(argv)
        return CommandResult(0, "b" * 40 + "\n")

    result = validate(root, expected_commit="a" * 40, runner=runner)
    assert not result["passed"] and result["error"] == "checkout_commit_mismatch"
    assert len(calls) == 1

    def failing(argv, *_):
        if "rev-parse" in argv:
            return CommandResult(0, "a" * 40)
        return CommandResult(1, stderr="Independent validation failed")

    result = validate(root, runner=failing)
    assert not result["passed"]
    assert result["steps"][-1]["stderr_tail"] == "Independent validation failed"
    assert all(step["name"] != "build" for step in result["steps"])


def test_smoke_imports_fresh_wheel_before_prepared_old_package(tmp_path):
    fresh = tmp_path / "fresh"
    venv.EnvBuilder(with_pip=False).create(fresh)
    python = fresh / "bin/python"
    queried = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    site = Path(queried.stdout.strip())
    package = site / "agentd"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'fresh wheel'\n")
    (package / "cli.py").write_text(
        "import argparse\ndef main():\n argparse.ArgumentParser().parse_args()\n"
    )
    dist = site / "agentd-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: agentd\nVersion: 0.1.0\n")
    (dist / "entry_points.txt").write_text(
        "[console_scripts]\nagentd=agentd.cli:main\n"
    )
    (fresh / "bin/agentd").write_text("from agentd.cli import main\nmain()\n")
    old = tmp_path / "old-site"
    (old / "agentd").mkdir(parents=True)
    (old / "agentd/__init__.py").write_text(
        "raise RuntimeError('old package imported')\n"
    )
    dependencies = json.dumps([str(old), sysconfig.get_path("purelib")])
    result = subprocess.run(
        [str(python), "-I", "-c", _SMOKE, dependencies, "0.1.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    mismatched = subprocess.run(
        [str(python), "-I", "-c", _SMOKE, dependencies, "0.2.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mismatched.returncode != 0
    (dist / "METADATA").write_text(
        "Name: agentd\nVersion: 0.1.0\nRequires-Dist: packaging>=10000\n"
    )
    stale_dependency = subprocess.run(
        [str(python), "-I", "-c", _SMOKE, dependencies, "0.1.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale_dependency.returncode != 0
    assert "dependency versions" in stale_dependency.stderr
