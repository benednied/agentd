"""Validate the repository's normative documentation contracts."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

from agentd.cli import build_parser
from agentd.domain.enums import JobState
from agentd.domain.transitions import ALLOWED_TRANSITIONS

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MARKDOWN_FILES = (ROOT / "README.md", *sorted(DOCS.rglob("*.md")))
FENCE_PATTERN = re.compile(
    r"```(?:bash|console|shell|sh)\s*\n(.*?)```", re.IGNORECASE | re.DOTALL
)
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
PLACEHOLDER_VALUES = {
    "AMOUNT": "1",
    "ARCH": "x86_64",
    "BUCKET": "primary",
    "CAPABILITY": "local-process",
    "CLASS": "standard",
    "COMMAND": "init",
    "COUNT": "1",
    "CPU": "1",
    "DEPENDENCY_ID": "dependency",
    "GB": "1",
    "HARNESS": "fake",
    "JOB_ID": "job",
    "MODEL": "standard",
    "N": "1",
    "NODE": "local",
    "NUMBER": "1",
    "OS": "linux",
    "PATH": "/tmp/agentd-docs",
    "POOL": "default",
    "POOL_ID": "default",
    "PROJECT": "example",
    "PROVIDER": "local-test",
    "RAM": "1",
    "REF": "HEAD",
    "REPOSITORY": "/tmp/agentd-repository",
    "RUN_ID": "run",
    "TEXT": "example",
    "UNIT": "tokens",
}


def _local_target(source: Path, target: str) -> tuple[Path, str | None]:
    path_part, _, fragment = target.partition("#")
    resolved = (source.parent / path_part).resolve() if path_part else source
    return resolved, fragment or None


def _heading_slug(value: str) -> str:
    value = re.sub(r"[`*_~]", "", value.casefold())
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", value.strip())


def _headings(path: Path) -> set[str]:
    return {
        _heading_slug(match.group(2))
        for match in re.finditer(
            r"^(#{1,6})\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE
        )
    }


def check_links() -> list[str]:
    errors: list[str] = []
    for source in MARKDOWN_FILES:
        content = source.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(content):
            target = raw_target.strip("<>")
            if not target or target.startswith(("http:", "https:", "mailto:")):
                continue
            resolved, fragment = _local_target(source, target)
            if not resolved.exists():
                errors.append(
                    f"{source.relative_to(ROOT)}: missing link target {target}"
                )
                continue
            if fragment and resolved.is_file() and fragment not in _headings(resolved):
                errors.append(f"{source.relative_to(ROOT)}: missing anchor {target}")
    return errors


def _logical_shell_lines(block: str) -> Iterable[str]:
    pending: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending.append(line[:-1].rstrip() if line.endswith("\\") else line)
        if not line.endswith("\\"):
            yield " ".join(pending)
            pending = []
    if pending:
        yield " ".join(pending)


def _replace_placeholders(tokens: list[str]) -> list[str]:
    return [PLACEHOLDER_VALUES.get(token, token) for token in tokens]


def check_cli_examples() -> list[str]:
    parser = build_parser()
    errors: list[str] = []
    for source in MARKDOWN_FILES:
        content = source.read_text(encoding="utf-8")
        for block in FENCE_PATTERN.findall(content):
            for line in _logical_shell_lines(block):
                marker = "uv run agentd"
                if marker not in line:
                    continue
                command = line[line.index(marker) :].replace(marker, "agentd", 1)
                try:
                    tokens = _replace_placeholders(shlex.split(command))
                    # argparse writes its usage text to the process streams before
                    # raising SystemExit for invalid examples. Keep the checker
                    # output concise and report the source line below instead.
                    with (
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        parser.parse_args(tokens[1:])
                except (
                    argparse.ArgumentError,
                    ValueError,
                    TypeError,
                    SystemExit,
                ) as error:
                    if isinstance(error, SystemExit) and error.code == 0:
                        continue
                    errors.append(
                        f"{source.relative_to(ROOT)}: CLI example does not parse: "
                        f"{line}"
                    )
    return errors


def check_state_machine() -> list[str]:
    source = DOCS / "80-reference" / "state-machine.md"
    errors: list[str] = []
    documented: dict[JobState, frozenset[JobState]] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2 or not cells[0].startswith("`"):
            continue
        try:
            state = JobState(cells[0].strip("`"))
            destinations = (
                frozenset()
                if cells[1] == "none"
                else frozenset(
                    JobState(item) for item in re.findall(r"`([^`]+)`", cells[1])
                )
            )
        except ValueError as error:
            errors.append(
                f"{source.relative_to(ROOT)}: invalid state table row: {error}"
            )
            continue
        documented[state] = destinations

    expected = {
        state: frozenset(destinations)
        for state, destinations in ALLOWED_TRANSITIONS.items()
    }
    if documented != expected:
        errors.append(
            f"{source.relative_to(ROOT)}: state table differs from ALLOWED_TRANSITIONS"
        )
    return errors


def check_configuration_reference() -> list[str]:
    source = ROOT / "src" / "agentd" / "config.py"
    reference = DOCS / "80-reference" / "configuration.md"
    source_names = set(
        re.findall(
            r'"((?:AGENTD|UV|XDG)_[A-Z0-9_]+|HOME)"',
            source.read_text(encoding="utf-8"),
        )
    )
    documented_names = set(
        re.findall(
            r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|",
            reference.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    missing = sorted(source_names - documented_names)
    extra = sorted(documented_names - source_names)
    errors: list[str] = []
    if missing:
        errors.append(f"{reference.relative_to(ROOT)}: missing variables {missing}")
    if extra:
        errors.append(f"{reference.relative_to(ROOT)}: unknown variables {extra}")
    return errors


def main() -> int:
    checks = (
        check_links,
        check_cli_examples,
        check_state_machine,
        check_configuration_reference,
    )
    errors = [error for check in checks for error in check()]
    if errors:
        print("Documentation checks failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print("Documentation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
