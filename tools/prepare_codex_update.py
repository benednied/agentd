#!/usr/bin/env python3
"""Prepare stable Codex pins; validation and PR publication belong to CI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SDK_PINS = (
    ("pyproject.toml", r"(openai-codex==)([0-9.]+)"),
    ("src/agentd/codex_versions.py", r'(PINNED_OPENAI_CODEX_VERSION = ")([0-9.]+)'),
    ("docs/30-architecture/harnesses.md", r"(openai-codex==)([0-9.]+)"),
)
CLI_PIN = ("Dockerfile", r"(ARG CODEX_VERSION=)([0-9.]+)")


def stable_version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError(f"Not a stable Codex version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def prepare(root: Path, sdk: str, cli: str) -> bool:
    """Validate all pins before writing; never downgrade or accept prereleases."""
    stable_version(sdk)
    stable_version(cli)
    changes = {}
    current_sdk = set()
    for name, pattern in (*SDK_PINS, CLI_PIN):
        text = (root / name).read_text()
        matches = list(re.finditer(pattern, text))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one Codex pin in {name}")
        old = matches[0][2]
        target = cli if name == "Dockerfile" else sdk
        if name != "Dockerfile":
            current_sdk.add(old)
        if stable_version(target) < stable_version(old):
            raise ValueError(f"Refusing Codex downgrade in {name}")
        changes[name] = re.sub(pattern, lambda m, target=target: m[1] + target, text)
    if len(current_sdk) != 1:
        raise ValueError("SDK and sandbox pins disagree")
    changed = False
    for name, text in changes.items():
        path = root / name
        if path.read_text() != text:
            path.write_text(text)
            changed = True
    return changed


def registry_version(url: str, *, pypi: bool = False) -> str:
    with urlopen(url, timeout=30) as response:
        data = json.load(response)
    value = data["info"]["version"] if pypi else data["version"]
    stable_version(value)
    return value


def main() -> None:
    sdk = registry_version("https://pypi.org/pypi/openai-codex/json", pypi=True)
    cli = registry_version("https://registry.npmjs.org/@openai%2fcodex/latest")
    prepare(ROOT, sdk, cli)
    print(f"Candidate SDK/bundled runtime: {sdk}; standalone CLI: {cli}")


if __name__ == "__main__":
    main()
