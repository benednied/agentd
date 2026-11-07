import stat
import subprocess
import sys
import venv
from dataclasses import replace
from pathlib import Path

import pytest

from agentd.candidate_runtime import (
    CandidatePreparationError,
    CandidateRuntimePreparation,
    lock_digest,
)


@pytest.fixture(autouse=True)
def cleanup_readonly_runtime(tmp_path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


class Installer:
    calls = 0

    def run(self, command, *, cwd, env, timeout):
        self.calls += 1
        assert "--offline" in command
        assert "--no-build" in command
        assert "--no-install-project" in command
        assert "--no-python-downloads" in command
        assert "--frozen" in command
        assert set(p.name for p in cwd.iterdir()) == {
            "uv.lock",
            "pyproject.toml",
            "cache",
        }
        venv.EnvBuilder(with_pip=False, symlinks=True).create(cwd / ".venv")
        return subprocess.CompletedProcess(command, 0, "", "")


def setup(tmp_path):
    checkout = tmp_path / "candidate"
    checkout.mkdir()
    (checkout / "uv.lock").write_text("version = 1\n")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1"\n'
    )
    (checkout / "untrusted.py").write_text('raise RuntimeError("must not execute")')
    cache = tmp_path / "cache"
    cache.mkdir()
    prep = CandidateRuntimePreparation(
        cache,
        Path(sys.executable),
        Path("/usr/bin/uv"),
        (Path(sys.base_prefix), Path(sys.executable).resolve().parent),
        Installer(),
    )
    return checkout, prep


def test_candidate_lock_change_builds_new_immutable_runtime(tmp_path):
    checkout, prep = setup(tmp_path)
    (checkout / "uv.lock").write_text("version = 1\n# legitimate change\n")
    artifact = prep.prepare(checkout, tmp_path / "runtime")
    assert artifact.lock_sha256 == lock_digest(checkout)
    artifact.verify(checkout)
    assert all(
        not p.stat().st_mode & stat.S_IWUSR
        for p in artifact.root.rglob("*")
        if not p.is_symlink()
    )
    assert (
        subprocess.check_output(
            [str(artifact.root / "venv/bin/python"), "-c", "print(7)"], text=True
        ).strip()
        == "7"
    )
    (checkout / "uv.lock").write_text("version = 1\n# different candidate\n")
    with pytest.raises(CandidatePreparationError, match="digest mismatch"):
        artifact.verify(checkout)


def test_receipt_mismatch_is_rejected(tmp_path):
    checkout, prep = setup(tmp_path)
    artifact = prep.prepare(checkout, tmp_path / "runtime")
    receipt = artifact.root / "lock.sha256"
    receipt.chmod(0o600)
    receipt.write_text("wrong")
    with pytest.raises(CandidatePreparationError, match="receipt"):
        artifact.verify(checkout)


@pytest.mark.parametrize(
    "source",
    [
        '{ git = "https://evil/repo" }',
        '{ registry = "https://evil/simple" }',
        '{ directory = "../other" }',
    ],
)
def test_unapproved_sources_never_reach_installer(tmp_path, source):
    checkout, prep = setup(tmp_path)
    (checkout / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "bad"\nsource = ' + source
    )
    with pytest.raises(CandidatePreparationError, match="Unapproved"):
        prep.prepare(checkout, tmp_path / "runtime")
    assert prep.runner.calls == 0


def test_symlink_lock_is_rejected(tmp_path):
    checkout, prep = setup(tmp_path)
    (checkout / "uv.lock").unlink()
    (checkout / "uv.lock").symlink_to(checkout / "pyproject.toml")
    with pytest.raises(CandidatePreparationError, match="regular"):
        prep.prepare(checkout, tmp_path / "runtime")


def test_prerequisite_changes_only_on_administrative_changes(tmp_path):
    _, prep = setup(tmp_path)
    assert replace(prep, revision="new-cache").prerequisite != prep.prerequisite


def test_real_uv_prepares_metadata_only_offline(tmp_path):
    import shutil

    checkout, prep = setup(tmp_path)
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv unavailable")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1"\nrequires-python = ">=3.12"\n'
    )
    (checkout / "uv.lock").unlink()
    subprocess.run(
        [uv, "lock", "--offline", "--no-config", "--python", sys.executable],
        cwd=checkout,
        check=True,
        capture_output=True,
    )

    class LocalFixtureRunner:
        # Real uv against only this test's metadata; production always uses bwrap.
        def run(self, command, *, cwd, env, timeout):
            command = [
                str(cwd / "cache") if p == "/workspace/cache" else p for p in command
            ]
            return subprocess.run(
                command,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
            )

    prep = replace(prep, uv=Path(uv), runner=LocalFixtureRunner())
    artifact = prep.prepare(checkout, tmp_path / "runtime")
    artifact.verify(checkout)
    assert (artifact.root / "venv/bin/python").exists()
