"""Repository toolchains preserve the deployment's narrow mount authority."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


def contract():
    path = (
        Path(__file__).resolve().parents[1]
        / "deploy/security/validate_coding_compose.py"
    )
    spec = importlib.util.spec_from_file_location("coding_compose_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployment(module):
    return {
        "name": "agentd-selfhost-coding",
        "services": {
            name: {
                "user": "1000:1000",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": [
                    "no-new-privileges:true",
                    "apparmor=lxc-usernsexec",
                    "seccomp=reviewed.json",
                ],
                "environment": {},
                "healthcheck": {"test": ["CMD", "health"]},
                "volumes": [
                    {
                        "type": "bind",
                        "source": f"/home/bened/.local/state/agentd-selfhost/{i}",
                        "target": target,
                        "read_only": readonly,
                        "bind": {"create_host_path": False},
                    }
                    for i, (target, readonly) in enumerate(targets.items())
                ],
            }
            for name, targets in module.TARGETS.items()
        },
    }


def test_optional_repository_runtime_is_shared_and_readonly():
    module = contract()
    config = deployment(module)
    module.validate(config)
    mount = {
        "type": "bind",
        "source": module.TOOLCHAIN_ROOT + "goldenage-frozen-v1",
        "target": module.REPOSITORY_RUNTIME,
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    for name in ("coding-worker", "coding-publisher"):
        config["services"][name]["volumes"].append(deepcopy(mount))
    module.validate(config)

    for role, changes in (
        ("coding-worker", {"read_only": False}),
        ("coding-publisher", {"source": module.TOOLCHAIN_ROOT + "other"}),
        ("coding-worker", {"source": module.TOOLCHAIN_ROOT + "../credentials"}),
        (
            "coding-publisher",
            {"source": "/home/bened/.local/state/agentd-selfhost/coding/github-read"},
        ),
    ):
        unsafe = deepcopy(config)
        unsafe["services"][role]["volumes"][-1].update(changes)
        with pytest.raises(AssertionError):
            module.validate(unsafe)

    missing = deepcopy(config)
    missing["services"]["coding-publisher"]["volumes"].pop()
    with pytest.raises(AssertionError):
        module.validate(missing)
