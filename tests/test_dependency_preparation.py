import stat
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


def test_read_only_dependencies_are_writable_only_in_private_copy(tmp_path):
    source = tmp_path / "prepared" / "venv"
    launcher = source / "bin" / "probe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{source}/bin/python\nprint('ok')\n")
    launcher.chmod(0o555)
    site = source / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    hook = site / "_virtualenv.pth"
    hook.write_text(f"import _virtualenv\n{source.parent}/src\n")
    hook.chmod(0o444)
    editable = site / "__editable__.example.pth"
    editable.write_text(str(source.parent / "src"))
    editable.chmod(0o444)
    module = site / "example.py"
    module.write_text("value = 7\n")
    module.chmod(0o444)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    interpreter = runtime / "python"
    interpreter.write_text("immutable interpreter")
    interpreter.chmod(0o555)
    (source / "bin" / "python").symlink_to(interpreter)
    (source / "bin" / "alias").symlink_to(launcher)
    (source / "runtime").symlink_to(runtime, target_is_directory=True)
    runtime.chmod(0o555)
    paths = [source, *source.rglob("*")]
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o555)
    original_modes = {path: path.lstat().st_mode for path in paths}
    original_bytes = {
        path: path.read_bytes()
        for path in paths
        if path.is_file() and not path.is_symlink()
    }
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    try:
        DependencyRuntimePreparation(
            source.parent, (PreparationMount(source, ".venv"),), (runtime,)
        ).prepare(worktree)
        copied = worktree / ".venv"
        assert (copied / "bin" / "probe").read_text().splitlines()[
            0
        ] == f"#!{copied}/bin/python"
        copied_site = copied / site.relative_to(source)
        assert (copied_site / hook.name).read_text() == "import _virtualenv\n"
        assert not (copied_site / editable.name).exists()
        assert (copied_site / module.name).read_bytes() == original_bytes[module]
        for path in paths:
            if not path.is_symlink() and path != editable:
                private = copied / path.relative_to(source)
                assert stat.S_IMODE(private.stat().st_mode) == (
                    stat.S_IMODE(original_modes[path]) | stat.S_IWUSR
                )
        assert (copied / "bin" / "alias").readlink() == copied / "bin" / "probe"
        assert (copied / "bin" / "python").readlink() == interpreter
        assert (copied / "runtime").readlink() == runtime
        assert stat.S_IMODE(interpreter.stat().st_mode) == 0o555
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o555
        assert interpreter.read_text() == "immutable interpreter"
        assert {path: path.lstat().st_mode for path in paths} == original_modes
        assert {path: path.read_bytes() for path in original_bytes} == original_bytes
    finally:
        for path in paths:
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        runtime.chmod(0o755)


@pytest.mark.parametrize("read_only", [False, True])
def test_real_venv_remains_executable_after_relocation(tmp_path, read_only):
    import subprocess
    import sys
    import venv

    source = tmp_path / "prepared" / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(source)
    (source / "bin" / "probe").write_text(f"#!{source}/bin/python\nprint('ok')\n")
    (source / "bin" / "probe").chmod(0o755)
    site = next(source.glob("lib/python*/site-packages"))
    (site / "__editable__.example.pth").write_text(str(source.parent / "src"))
    (site / "_virtualenv.pth").write_text("import sys\n")
    paths = [source, *source.rglob("*")]
    if read_only:
        for path in paths:
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
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
    ] == f"#!{copied}/bin/python"
    assert not list(copied.glob("lib/python*/site-packages/__editable__*.pth"))
    assert stat.S_IMODE((copied / "bin" / "probe").stat().st_mode) == 0o755
    probe = subprocess.run(
        [str(copied / "bin" / "python"), "-I", str(copied / "bin" / "probe")],
        check=True,
        text=True,
        capture_output=True,
    )
    assert probe.stdout.strip() == "ok"
    if read_only:
        assert all(
            not path.stat().st_mode & 0o222 for path in paths if not path.is_symlink()
        )
        for path in paths:
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
