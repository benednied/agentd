import hashlib
import json
import subprocess
from collections import Counter
from io import StringIO

import pytest
from loguru import logger

from agentd.coding.integration import GitHubIntegrationAdapter
from agentd.intake.github_rest import GitHubRateLimited, GitHubREST
from agentd.intake.workflow import GitHubWorkflowSource
from agentd.observability import configure_logging

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


@pytest.fixture
def rest_logs():
    output = StringIO()
    configure_logging()
    logger.remove()
    logger.add(output, serialize=True, enqueue=False, level="INFO")
    try:
        yield output
    finally:
        logger.remove()
        logger.disable("agentd")


def log_records(output):
    return [json.loads(line)["record"] for line in output.getvalue().splitlines()]


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


def test_logs_show_shared_limit_and_recovery_without_private_content(
    tmp_path, monkeypatch, rest_logs
):
    path = tmp_path / "budget"
    now = 100
    controller = GitHubREST(
        database=path, component="controller", repository=REPOSITORY, clock=lambda: now
    )
    publisher = GitHubREST(database=path, component="publisher", clock=lambda: now)

    def github(command, **kwargs):
        if now >= 300:
            return response(command, 200, {"id": 42}, x_ratelimit_remaining=4999)
        return response(
            command,
            403,
            {"message": "rate limit exceeded", "body": "private-response-body"},
            x_ratelimit_limit=5000,
            x_ratelimit_remaining=0,
            x_ratelimit_reset=240,
            x_github_request_id="ABCD:2345:6789:9876",
            retry_after=200,
        )

    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", github)
    with pytest.raises(GitHubRateLimited):
        controller.request("source_issues", REPO + "?query=private-query")
    for _ in range(2):
        with pytest.raises(GitHubRateLimited):
            publisher.check_budget("publisher_poll")
    now = 300
    publisher.request("integration_repository", REPO)

    records = log_records(rest_logs)
    assert [entry["message"] for entry in records] == [
        "github_rest_backoff_set",
        "github_rest_deferred",
        "github_rest_resumed",
    ]
    started, deferred, resumed = (entry["extra"] for entry in records)
    assert started == {
        "component": "controller",
        "operation": "source_issues",
        "repository": REPOSITORY,
        "http_method": "GET",
        "http_status": 403,
        "conditional_sent": False,
        "duration_ms": started["duration_ms"],
        "error_type": "rate_limited",
        "github_request_id": "ABCD:2345:6789:9876",
        "rate_limit": 5000,
        "rate_remaining": 0,
        "rate_reset_at": 240,
        "retry_after_at": 300,
        "rate_limit_kind": "primary",
        "retry_at": 300,
    }
    assert deferred["component"] == resumed["component"] == "publisher"
    assert deferred["retry_at"] == resumed["retry_at"] == 300
    assert resumed["http_status"] == 200
    assert "private-" not in rest_logs.getvalue()


def test_logs_distinguish_failures_and_summarize_quiet_requests(monkeypatch, rest_logs):
    now = 0
    rest = GitHubREST(component="controller", clock=lambda: now)

    def github(command, **kwargs):
        if command[5] == "forbidden":
            return response(command, 403, x_github_request_id="private-request-id")
        if command[5] == "offline":
            return subprocess.CompletedProcess(
                command, 1, "", "error connecting: private-hostname"
            )
        status = 304 if 'If-None-Match: "private-etag"' in command else 200
        return response(command, status, {"id": 42}, etag='"private-etag"')

    monkeypatch.setattr("agentd.intake.github_rest.subprocess.run", github)
    for _ in range(2):
        with pytest.raises(subprocess.CalledProcessError):
            rest.request("source_authority", "forbidden")
    with pytest.raises(subprocess.CalledProcessError):
        rest.request("source_authority", "offline")
    rest.request("source_issues", REPO, conditional=True)
    now = 900
    rest.request("source_issues", REPO, conditional=True)

    records = log_records(rest_logs)
    assert [entry["message"] for entry in records] == [
        "github_rest_request_failed",
        "github_rest_request_failed",
        "github_rest_request_summary",
        "github_rest_request_summary",
    ]
    assert [entry["extra"]["error_type"] for entry in records[:2]] == [
        "forbidden",
        "transport_error",
    ]
    assert records[-2]["extra"]["failed_count"] == 3
    assert records[-1]["extra"] == {
        "component": "controller",
        "operation": "source_issues",
        "window_started_at": 0,
        "window_ended_at": 900,
        "request_count": 2,
        "conditional_count": 1,
        "not_modified_count": 1,
        "failed_count": 0,
    }
    assert "private-" not in rest_logs.getvalue()
