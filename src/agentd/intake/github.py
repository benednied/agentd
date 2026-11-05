"""Read-only GitHub connector using the operator's installed gh authentication."""

from __future__ import annotations

from typing import Any

from agentd.coding.models import repository_name
from agentd.intake.github_rest import GitHubREST
from agentd.intake.models import SourceIssue
from agentd.observability import event_logger


class GitHubIssueSource:
    def __init__(self, rest: GitHubREST | None = None) -> None:
        self.rest = rest or GitHubREST()

    def _get(self, endpoint: str) -> Any:
        return self.rest.request("source_authority", endpoint)

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

    def poll(
        self,
        repository: str,
        *,
        max_pages: int = 10,
        tracked: tuple[SourceIssue, ...] = (),
    ) -> tuple[SourceIssue, ...]:
        repository = repository_name(repository)
        repository_id = int(
            self.rest.request(
                "source_repository", f"repos/{repository}", conditional=True
            )["id"]
        )
        result = []
        for page in range(1, max_pages + 1):
            items = self.rest.request(
                "source_issues",
                f"repos/{repository}/issues?state=all&per_page=100&page={page}",
                conditional=True,
            )
            result.extend(
                self._decode(repository, repository_id, item) for item in items
            )
            if len(items) < 100:
                break
        else:
            event_logger(
                component=self.rest.component,
                operation="source_issues",
                repository=repository,
                page_limit=max_pages,
            ).warning("github_poll_incomplete")
            raise ValueError("GitHub issue pagination is incomplete")
        # Moving pages can miss a tracked issue. Reconcile it directly using
        # this sweep's verified repository identity, never absence alone.
        seen = {issue.key for issue in result}
        for previous in tracked:
            if previous.key in seen:
                continue
            item = self.rest.request(
                "source_tracked_issue",
                f"repos/{repository}/issues/{previous.number}",
                conditional=True,
            )
            issue = self._decode(repository, repository_id, item)
            if issue.key != previous.key:
                event_logger(
                    component=self.rest.component,
                    operation="source_tracked_issue",
                    repository=repository,
                ).warning("github_source_identity_changed")
                raise ValueError("Tracked GitHub issue identity changed")
            result.append(issue)
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
            author_login=data.get("user", {}).get("login"),
            author_id=data.get("user", {}).get("id"),
            created_at=data.get("created_at"),
        )
