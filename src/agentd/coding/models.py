"""Portable, credential-free coding policy and work identity.

Profiles are administrative configuration; source text supplies only intent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path
from typing import Any


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def repository_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("repository must be an owner/name identity")
    return value.lower()


def exact_commit(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise ValueError("base_commit must be a complete lowercase Git commit")
    return value


@dataclass(frozen=True, slots=True)
class RepositoryProfile:
    id: str
    version: str
    repository: str
    clone_url: str
    harnesses: tuple[str, ...] = ("codex",)
    required_capabilities: tuple[str, ...] = ("remote-coding",)
    validation_commands: tuple[tuple[str, ...], ...] = ()
    max_runtime_seconds: float = 600
    validation_timeout_seconds: float = 300
    network_policy: str = "disabled"
    preparation_strategy: str = "git-worktree"
    filesystem_policy: str = "workspace-write"

    def __post_init__(self) -> None:
        for value in (self.id, self.version):
            if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", value) is None:
                raise ValueError("profile identity/version must be stable names")
        if self.repository != repository_name(self.repository):
            raise ValueError("repository identity must be lowercase")
        if self.clone_url != f"https://github.com/{self.repository}.git":
            raise ValueError("profile clone URL must match the GitHub repository")
        if not self.harnesses or any(not item for item in self.harnesses):
            raise ValueError("profile requires supported harnesses")
        if self.preparation_strategy != "git-worktree":
            raise ValueError("unsupported preparation strategy")
        if self.filesystem_policy != "workspace-write":
            raise ValueError("unsupported filesystem policy")
        if self.network_policy not in {"disabled", "provider-only"}:
            raise ValueError("unsupported network policy")
        for limit in (self.max_runtime_seconds, self.validation_timeout_seconds):
            if not isfinite(limit) or limit <= 0:
                raise ValueError("profile runtime limits must be finite and positive")
        for command in self.validation_commands:
            if not command or any(not arg or "\0" in arg for arg in command):
                raise ValueError("validation commands require nonempty argv")

    @property
    def digest(self) -> str:
        return fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositoryProfile:
        return cls(
            **{
                **data,
                "harnesses": tuple(data.get("harnesses", ("codex",))),
                "required_capabilities": tuple(
                    data.get("required_capabilities", ("remote-coding",))
                ),
                "validation_commands": tuple(
                    tuple(argv) for argv in data.get("validation_commands", ())
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class CodingWorkOrder:
    job_id: str
    repository: str
    profile_id: str
    profile_version: str
    profile_digest: str
    base_commit: str
    source_revision: str
    objective: str
    harness: str
    account_pool_id: str
    expected_quota: float
    maximum_quota: float
    max_runtime_seconds: float
    acceptance_criteria: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ("remote-coding",)
    context_references: tuple[str, ...] = ()
    quota_unit: str = "tokens"

    def __post_init__(self) -> None:
        repository_name(self.repository)
        exact_commit(self.base_commit)
        for digest in (self.profile_digest, self.source_revision):
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError(
                    "work order requires exact source/profile fingerprints"
                )
        for value in (
            self.job_id,
            self.profile_id,
            self.profile_version,
            self.harness,
            self.account_pool_id,
        ):
            if not value or "\0" in value:
                raise ValueError("work order requires stable identities")
        if any(
            not isfinite(n) or n <= 0
            for n in (self.expected_quota, self.maximum_quota, self.max_runtime_seconds)
        ):
            raise ValueError("work order requires finite positive limits")
        if self.maximum_quota < self.expected_quota or self.quota_unit != "tokens":
            raise ValueError("work order requires a bounded token budget")
        if any(Path(ref).is_absolute() for ref in self.context_references):
            raise ValueError("context references must not contain controller paths")

    def validate_profile(self, profile: RepositoryProfile) -> None:
        if (
            self.repository,
            self.profile_id,
            self.profile_version,
            self.profile_digest,
        ) != (profile.repository, profile.id, profile.version, profile.digest):
            raise ValueError("repository profile mismatch")
        if self.harness not in profile.harnesses:
            raise ValueError("unsupported coding harness")
        if not set(profile.required_capabilities).issubset(self.required_capabilities):
            raise ValueError("work order omits required profile capabilities")
        if self.max_runtime_seconds > profile.max_runtime_seconds:
            raise ValueError("work order exceeds profile runtime limit")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodingWorkOrder:
        return cls(
            **{
                **data,
                "acceptance_criteria": tuple(data.get("acceptance_criteria", ())),
                "required_capabilities": tuple(
                    data.get("required_capabilities", ("remote-coding",))
                ),
                "context_references": tuple(data.get("context_references", ())),
            }
        )
