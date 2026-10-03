from pathlib import Path

import pytest

from agentd.workers.dependency_prep import (
    DependencyPreparationError,
    DependencyRuntimePreparation,
    PreparationMount,
)


def test_preparation_copies_only_explicit_mounts(tmp_path: Path):
    source = tmp_path / "approved" / "venv"
    source.mkdir(parents=True)
    (source / "bin").mkdir()
    (source / "bin" / "python").write_text("runtime")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = DependencyRuntimePreparation(
        tmp_path / "approved", (PreparationMount(source, ".venv"),)
    ).prepare(worktree)
    assert result == (worktree / ".venv",)
    assert (worktree / ".venv" / "bin" / "python").read_text() == "runtime"


def test_preparation_rejects_escape_and_credentials(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(DependencyPreparationError, match="escapes"):
        DependencyRuntimePreparation(
            tmp_path / "allowed", (PreparationMount(tmp_path / "other", "deps"),)
        ).prepare(worktree)
    secret = tmp_path / "allowed" / "credentials"
    secret.mkdir(parents=True)
    with pytest.raises(DependencyPreparationError, match="credential"):
        DependencyRuntimePreparation(
            tmp_path / "allowed", (PreparationMount(secret, "deps"),)
        ).prepare(worktree)


def test_preparation_rejects_malicious_nested_symlink(tmp_path: Path):
    source = tmp_path / "allowed" / "venv"
    source.mkdir(parents=True)
    (source / "payload").symlink_to(tmp_path / "outside")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(DependencyPreparationError, match="symlink"):
        DependencyRuntimePreparation(
            tmp_path / "allowed", (PreparationMount(source, ".venv"),)
        ).prepare(worktree)


def test_real_venv_remains_executable_after_relocation(tmp_path):
    import subprocess
    import sys
    import venv

    source = tmp_path / "prepared" / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(source)
    (source / "bin" / "probe").write_text(f"#!{source}/bin/python3.12\nprint('ok')\n")
    site = next(source.glob("lib/python*/site-packages"))
    (site / "__editable__.example.pth").write_text(str(source.parent / "src"))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    DependencyRuntimePreparation(
        source.parent,
        (PreparationMount(source, ".venv"),),
        (Path(sys.base_prefix), Path(sys.executable).resolve().parent),
    ).prepare(worktree)
    copied = worktree / ".venv"
    result = subprocess.run(
        [str(copied / "bin" / "python"), "-c", "import sys; print(sys.prefix)"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == str(copied)
    assert (copied / "bin" / "probe").read_text().splitlines()[
        0
    ] == f"#!{copied}/bin/python3.12"
    assert not list(copied.glob("lib/python*/site-packages/__editable__*.pth"))
