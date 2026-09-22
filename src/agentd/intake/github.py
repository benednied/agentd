"""Read-only GitHub connector using the operator's installed gh authentication."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from agentd.coding.models import repository_name
from agentd.intake.models import SourceIssue


class GitHubIssueSource:
    def _get(self, endpoint: str) -> Any:
        completed = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(completed.stdout)

    def get(self, repository: str, number: int) -> SourceIssue:
        repository = repository_name(repository)
        if number <= 0:
            raise ValueError("issue number must be positive")
        repo = self._get(f"repos/{repository}")
        return self._decode(
            repository,
            int(repo["id"]),
            self._get(f"repos/{repository}/issues/{number}"),
        )

    def poll(self, repository: str, *, max_pages: int = 10) -> tuple[SourceIssue, ...]:
        repository = repository_name(repository)
        repo = self._get(f"repos/{repository}")
        result = []
        for page in range(1, max_pages + 1):
            items = self._get(
                f"repos/{repository}/issues?state=all&per_page=100&page={page}"
            )
            result.extend(
                self._decode(repository, int(repo["id"]), item) for item in items
            )
            if len(items) < 100:
                return tuple(result)
        # Never infer cancellation from absence in a bounded/incomplete poll.
        return tuple(result)

    @staticmethod
    def _decode(
        repository: str, repository_id: int, data: dict[str, Any]
    ) -> SourceIssue:
        return SourceIssue(
            repository=repository,
            repository_id=repository_id,
            number=int(data["number"]),
            node_id=str(data["node_id"]),
            title=str(data["title"]),
            body=data.get("body") or "",
            updated_at=str(data["updated_at"]),
            state=str(data["state"]),
            labels=tuple(str(label["name"]) for label in data.get("labels", [])),
            is_pull_request="pull_request" in data,
        )
