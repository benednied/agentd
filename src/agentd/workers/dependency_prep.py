"""Explicit, credential-free preparation of configured coding dependencies."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class DependencyPreparationError(RuntimeError):
    """Configured dependency materialization failed closed."""


@dataclass(frozen=True, slots=True)
class PreparationMount:
    source: Path
    destination: str


@dataclass(frozen=True, slots=True)
class DependencyRuntimePreparation:
    """Copy only operator-configured, credential-free runtime artifacts."""

    allowed_root: Path
    mounts: tuple[PreparationMount, ...] = ()
    runtime_roots: tuple[Path, ...] = ()

    def prepare(self, worktree: Path) -> tuple[Path, ...]:
        root = self.allowed_root.resolve()
        target = worktree.resolve()
        if not target.is_dir():
            raise DependencyPreparationError("coding worktree does not exist")
        prepared: list[Path] = []
        for mount in self.mounts:
            if mount.source.is_symlink():
                raise DependencyPreparationError(
                    "dependency source root must not be a symlink"
                )
            source = mount.source.resolve()
            try:
                source.relative_to(root)
            except ValueError as error:
                raise DependencyPreparationError(
                    "dependency source escapes configured root"
                ) from error
            if not source.exists():
                raise DependencyPreparationError(
                    "dependency source must be an existing non-symlink"
                )
            destination = (target / mount.destination).resolve()
            try:
                destination.relative_to(target)
            except ValueError as error:
                raise DependencyPreparationError(
                    "dependency destination escapes worktree"
                ) from error
            if mount.destination in {"", ".", ".."}:
                raise DependencyPreparationError(
                    "dependency destination must be a relative project path"
                )
            if any(
                part.lower() in {".env", "credentials", "credential", "secrets"}
                for part in source.parts
            ):
                raise DependencyPreparationError(
                    "credential-looking dependency source is forbidden"
                )
            if destination.exists() or destination.is_symlink():
                raise DependencyPreparationError(
                    "dependency destination already exists"
                )
            self._validate_tree(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
            self._relocate(destination, source)
            prepared.append(destination)
        return tuple(prepared)

    def _validate_tree(self, source: Path) -> None:
        for directory, dirs, files in os.walk(source, followlinks=False):
            for name in (*dirs, *files):
                path = Path(directory) / name
                if path.is_symlink():
                    target = os.readlink(path)
                    resolved = (path.parent / target).resolve()
                    trusted = tuple(root.resolve() for root in self.runtime_roots)
                    if not (
                        resolved.is_relative_to(source)
                        or any(resolved.is_relative_to(root) for root in trusted)
                    ):
                        raise DependencyPreparationError(
                            "nested dependency symlink escapes immutable runtime roots"
                        )

    @staticmethod
    def _relocate(destination: Path, source: Path) -> None:
        if not destination.is_dir():
            return
        for path in destination.rglob("*"):
            if path.is_symlink():
                link = os.readlink(path)
                if os.path.isabs(link) and Path(link).is_relative_to(source):
                    path.unlink()
                    path.symlink_to(destination / Path(link).relative_to(source))
                continue
            if not path.is_file():
                continue
            if path.name.startswith("__editable__") and path.suffix == ".pth":
                # Editable installation points at the prepared source checkout.
                # The project under test must supply its own package via cwd/src.
                path.unlink()
            elif path.suffix == ".pth":
                lines = path.read_text(errors="replace").splitlines()
                path.write_text(
                    "\n".join(
                        line
                        for line in lines
                        if not line.startswith(str(source.parent))
                    )
                    + "\n"
                )
            elif path.parent.name == "bin":
                data = path.read_bytes()
                first, separator, rest = data.partition(b"\n")
                if first.startswith(b"#!" + str(source).encode() + b"/"):
                    path.write_bytes(
                        first.replace(
                            str(source).encode(), str(destination).encode(), 1
                        )
                        + separator
                        + rest
                    )


__all__ = [
    "DependencyPreparationError",
    "DependencyRuntimePreparation",
    "PreparationMount",
]
