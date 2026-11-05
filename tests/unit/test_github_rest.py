import hashlib
import json
import subprocess
from collections import Counter

import pytest

from agentd.coding.integration import GitHubIntegrationAdapter
from agentd.intake.github_rest import GitHubRateLimited, GitHubREST
from agentd.intake.workflow import GitHubWorkflowSource

REPOSITORY = "owner/repo"
REPO = f"repos/{REPOSITORY}"
ISSUES = f"{REPO}/issues?state=all&per_page=100&page="


def response(command, status, data=None, **headers):
    header_lines = "".join(
        f"{key.replace('_', '-')}: {value}\n" for key, value in headers.items()
    )
    body = json.dumps(data) if data is not None else ""
    return subprocess.CompletedProcess(
        command, int(status >= 300), f"HTTP/2.0 {status}\n{header_lines}\n{body}", ""
    )


def issue(number, **updates):
    return {
        "number": number,
        "node_id": f"I_{number}",
        "title": f"Issue {number}",
        "state": "open",
        "updated_at": "2026-11-05T20:00:00Z",
        **updates,
    }


class QuietGitHub:
    def __init__(self, pages):
        self.pages = pages
        self.remaining = 5000
        self.calls = []

    def __call__(self, command, **kwargs):
        endpoint = command[5]
        data = self.pages[endpoint]
        etag = '"' + hashlib.sha256(json.dumps(data).encode()).hexdigest() + '"'
        status = 304 if f"If-None-Match: {etag}" in command else 200
        if status == 200:
            self.remaining -= 1
        self.calls.append((endpoint, status))
        return response(
            command,
            status,
            data if status == 200 else None,
            etag=etag,
            x_ratelimit_remaining=self.remaining,
            x_ratelimit_reset=3600,
        )


def test_quiet_hp_replay_leaves_headroom_for_a_full_window(tmp_path, monkeypatch):
    path = tmp_path / "github-rest"
    now = 0
    controller = GitHubREST(database=path, component="controller", clock=lambda: now)
    publisher = GitHubREST(database=path, component="publisher", clock=lambda: now)
    source = GitHubWorkflowSource(controller)
    integration = GitHubIntegrationAdapter(publisher)
    since = "2026-11-05T20:00:00Z"
    pages = {
        REPO: {"id": 42},
        ISSUES + "1": [issue(number) for number in range(1, 29)],
        f"{REPO}/branches/master": {"commit": {"sha": "a" * 40}},
        **{
            f"{REPO}/{collection}/comments?since=2026-11-05T20%3A00%3A00Z"
            "&per_page=100&page=1": []
            for collection in ("issues", "pulls")
        },
        **{
            f"{REPO}/pulls/{number}/reviews?per_page=100&page=1": []
            for number in range(30, 56)
        },
        **{
            f"{REPO}/pulls/{number}": {"number": number, "state": "open"}
            for number in range(30, 55)
        },
    }
    api = QuietGitHub(pages)
    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", api)
    tracked = tuple(
        source._decode(REPOSITORY, 42, item) for item in pages[ISSUES + "1"]
    )
    for tick_time in range(0, 3600, 60):
        now = tick_time
        controller.request("source_base", f"{REPO}/branches/master", conditional=True)
        assert source.poll(REPOSITORY, tracked=tracked) == tracked
        for kind in ("comment", "review-comment"):
            assert source.comments(REPOSITORY, since=since, kind=kind) == ()
        for number in range(30, 56):
            assert source.reviews(REPOSITORY, number) == ()
        assert integration.repository(REPOSITORY)["id"] == 42
        for number in range(30, 55):
            integration.pull_request(REPOSITORY, number, conditional=True)
            integration.reviews(REPOSITORY, number, conditional=True)

    stats = GitHubREST.diagnostics(path, now=now)
    counts = Counter()
    for item in stats["requests"]:
        counts[item["component"], item["outcome"]] += item["count"]
    assert counts == {
        ("controller", "200"): 31,
        ("controller", "304"): 1829,
        ("publisher", "200"): 51,
        ("publisher", "304"): 3009,
    }
    assert stats["remaining"] == 4918
    assert stats["backoff"] is None


def test_poll_reconciles_pages_and_checks_missing_tracked_issues(tmp_path, monkeypatch):
    rest = GitHubREST(database=tmp_path / "budget", clock=lambda: 0)
    source = GitHubWorkflowSource(rest)
    api = QuietGitHub(
        {
            REPO: {"id": 42},
            ISSUES + "1": [issue(number) for number in range(1, 101)],
            ISSUES + "2": [issue(101)],
            f"{REPO}/issues/102": issue(102, state="closed"),
        }
    )
    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", api)
    tracked = tuple(source._decode(REPOSITORY, 42, issue(n)) for n in (1, 101, 102))
    with pytest.raises(ValueError, match="pagination is incomplete"):
        source.poll(REPOSITORY, max_pages=1, tracked=tracked)
    first = source.poll(REPOSITORY, tracked=tracked)
    assert first[-1].number == 102 and first[-1].state == "closed"
    api.pages[ISSUES + "2"] = [issue(101, body="edited")]
    second = source.poll(REPOSITORY, tracked=tracked)
    assert second[-2].body == "edited"
    assert api.calls[-4:] == [
        (REPO, 304),
        (ISSUES + "1", 304),
        (ISSUES + "2", 200),
        (f"{REPO}/issues/102", 304),
    ]


@pytest.mark.parametrize(
    ("status", "headers", "kind", "deadline"),
    [
        (403, {"x_ratelimit_remaining": 0, "x_ratelimit_reset": 240}, "primary", 240),
        (429, {"retry_after": 90}, "secondary", 190),
        (403, {}, "secondary", 160),
    ],
)
def test_limit_is_shared_across_components_and_survives_restart(
    tmp_path, monkeypatch, status, headers, kind, deadline
):
    path = tmp_path / "budget"
    now = 100
    controller = GitHubREST(database=path, component="controller", clock=lambda: now)
    calls = []

    def github(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return response(command, status, {"message": "API rate limit"}, **headers)
        return response(command, 200, {"id": 42})

    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", github)
    with pytest.raises(GitHubRateLimited) as limit:
        controller.request("source_repository", REPO)
    assert (limit.value.kind, limit.value.retry_at) == (kind, deadline)

    publisher = GitHubREST(database=path, component="publisher", clock=lambda: now)
    now = deadline - 1
    with pytest.raises(GitHubRateLimited):
        publisher.request("integration_repository", REPO)
    assert len(calls) == 1
    now = deadline
    assert publisher.request("integration_repository", REPO) == {"id": 42}
    assert len(calls) == 2
    assert GitHubREST.diagnostics(path, now=now)["backoff"] is None


def test_boundary_read_bypasses_validator_and_auth_failure_never_serves_cache(
    tmp_path, monkeypatch
):
    rest = GitHubREST(database=tmp_path / "budget", clock=lambda: 0)
    calls = []

    def github(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return response(command, 200, {"id": 42}, etag='"v1"')
        return response(command, 403, {"message": "Resource not accessible"})

    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", github)
    assert rest.request("source_repository", REPO, conditional=True) == {"id": 42}
    with pytest.raises(subprocess.CalledProcessError):
        rest.request("source_authority", REPO)
    assert "--header" not in calls[-1]
    with pytest.raises(subprocess.CalledProcessError):
        rest.request("source_repository", REPO, conditional=True)
    assert 'If-None-Match: "v1"' in calls[-1]
