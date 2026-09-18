"""GitHub source observations and explicit administrative approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from agentd.coding.models import fingerprint, repository_name


@dataclass(frozen=True, slots=True)
class SourceIssue:
    repository: str
    repository_id: int
    number: int
    node_id: str
    title: str
    body: str
    updated_at: str
    state: str = "open"
    labels: tuple[str, ...] = ()
    is_pull_request: bool = False

    def __post_init__(self) -> None:
        if self.repository != repository_name(self.repository):
            raise ValueError("repository must be canonical lowercase owner/name")
        if self.repository_id <= 0 or self.number <= 0 or not self.node_id:
            raise ValueError("source requires stable GitHub identities")
        datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        if self.state not in {"open", "closed"}:
            raise ValueError("invalid issue state")

    @property
    def key(self) -> str:
        return f"github:{self.repository_id}:{self.node_id}"

    @property
    def revision(self) -> str:
        # updated_at also changes for labels/comments. Approval binds material intent.
        return fingerprint(
            {
                "key": self.key,
                "repository": self.repository,
                "number": self.number,
                "title": self.title,
                "body": self.body,
            }
        )

    @property
    def job_id(self) -> str:
        return "github-" + fingerprint(self.key)[:32]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceIssue:
        return cls(**{**data, "labels": tuple(data.get("labels", ()))})


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    repository: str
    repository_id: int
    eligibility_label: str = "agentd:approved"

    def __post_init__(self) -> None:
        if (
            self.repository != repository_name(self.repository)
            or self.repository_id <= 0
        ):
            raise ValueError("policy requires a canonical repository and immutable ID")
        if not self.eligibility_label:
            raise ValueError("policy requires an explicit eligibility label")

    def eligible(self, issue: SourceIssue) -> bool:
        return (
            issue.repository == self.repository
            and issue.repository_id == self.repository_id
            and issue.state == "open"
            and not issue.is_pull_request
            and self.eligibility_label in issue.labels
        )
