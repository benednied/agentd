"""Trusted host supervisor for reviewed agentd master releases.

Run outside worker/model authority. This process owns Docker builds and service
activation; a coding worker receives neither these credentials nor a socket.
Only reviewed fast-forward master history beyond the configured baseline is
eligible. Every failure retains the existing release and durable database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE = "5d3aba3212957b8628a736f12f22eeb854357d9a"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REQUIRED = frozenset(
    {
        "Dockerfile",
        "src/agentd/intake/workflow.py",
        "src/agentd/runtime/allowance.py",
        "src/agentd/runtime/health.py",
        "tools/validate_python_package.py",
        "tools/watch_selfhost_release.py",
        "tools/host_abandon.py",
        "deploy/scripts/coding-compose.sh",
        "deploy/scripts/coding-release.sh",
        "deploy/systemd/agentd-selfhost-controller.service",
        "deploy/systemd/agentd-selfhost-worker.service",
        "deploy/systemd/agentd-selfhost-publisher.service",
        "deploy/systemd/agentd-selfhost-release.service",
    }
)
_PACKAGE_GATE = """import pathlib,sys,tempfile
from agentd.publication import BubblewrapValidationRunner
mounts=tuple(map(pathlib.Path,('/opt/agentd/validation-venv','/opt/agentd/tools',
                             '/usr/local')))
runner=BubblewrapValidationRunner(runtime_mounts=mounts)
with tempfile.TemporaryDirectory(prefix='outside-validation-') as temp:
    secret=pathlib.Path(temp)/'secret'
    secret.write_text('synthetic-containment-secret')
    probe=f'''import os,pathlib,socket
assert 'GH_TOKEN' not in os.environ
assert not pathlib.Path('/proc/1/environ').exists()
try:
    pathlib.Path('.git/config').write_text('tampered')
except OSError:
    pass
else:
    raise AssertionError('Git metadata writable')
try:
    pathlib.Path({str(secret)!r}).read_text()
except OSError:
    pass
else:
    raise AssertionError('External state readable')
s=socket.socket()
s.settimeout(2)
try:
    assert s.connect_ex(('192.0.2.1',443))!=0
except OSError:
    pass
'''
    tested=runner.run(('/opt/agentd/validation-venv/bin/python','-I','-c',probe),
                      cwd=pathlib.Path('/candidate'),
                      env={'PATH':'/usr/bin:/bin','GH_TOKEN':'synthetic-probe'},
                      timeout=20)
    if tested.returncode:
        print('Sandbox containment qualification failed',file=sys.stderr)
        sys.exit(1)
result=runner.run(('/opt/agentd/validation-venv/bin/python','-I',
                  '/opt/agentd/tools/validate_python_package.py','--python',
                  '/opt/agentd/validation-venv/bin/python','--expected-commit',
                  sys.argv[1]),cwd=pathlib.Path('/candidate'),
                  env={'PATH':'/usr/bin:/bin'},timeout=600)
print(result.stdout)
print(result.stderr,file=sys.stderr)
sys.exit(result.returncode)
"""


class ReleaseBlocked(RuntimeError):
    """An explicit release gate could not be proved."""


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".release-status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with suppress(FileNotFoundError):
            Path(name).unlink()


def pages(output: str) -> list[dict[str, Any]]:
    """Decode every page after a successful gh --paginate command completion."""
    decoder = json.JSONDecoder()
    documents = []
    position = 0
    while position < len(output):
        if output[position].isspace():
            position += 1
            continue
        parsed, position = decoder.raw_decode(output, position)
        documents.append(parsed)
    if not documents:
        raise ReleaseBlocked("github_response_invalid")
    # Also accept slurped output from newer protected wrappers.
    if (
        len(documents) == 1
        and isinstance(documents[0], list)
        and all(isinstance(page, list) for page in documents[0])
    ):
        documents = documents[0]
    flattened = []
    for page in documents:
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ReleaseBlocked("github_response_invalid")
        flattened.extend(page)
    return flattened


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Bootstrap timestamps require UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Bootstrap timestamps require UTC")
    return parsed


def bootstrap_approval(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    *,
    sha: str,
    activated_at: str,
    actors: frozenset[str],
    actor_ids: Mapping[str, int],
) -> bool:
    """Require an explicit immutable maintainer decision for this bootstrap head."""
    if (pr.get("head") or {}).get("sha") != sha or not actor_ids:
        return False

    def trusted(actor: dict[str, Any]) -> bool:
        login = actor.get("login")
        identity = actor.get("id")
        return (
            login in actors
            and isinstance(identity, int)
            and not isinstance(identity, bool)
            and actor_ids.get(login) == identity
        )

    decisions = {}
    for review in sorted(
        reviews, key=lambda row: (row.get("submitted_at") or "", row.get("id", 0))
    ):
        if trusted(review.get("user") or {}) and review.get("state") in {
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
        }:
            decisions[review["user"]["login"]] = review
    # An issue comment cannot bypass an unresolved native changes request.
    if any(row.get("state") == "CHANGES_REQUESTED" for row in decisions.values()):
        return False
    activated = _utc(activated_at)
    for row in decisions.values():
        if row.get("state") == "APPROVED" and row.get("commit_id") == sha:
            try:
                if _utc(row["submitted_at"]) > activated:
                    return True
            except (ValueError, TypeError, KeyError):
                continue
    for comment in comments:
        body = comment.get("body")
        if not trusted(comment.get("user") or {}) or not isinstance(body, str):
            continue
        if (
            "<!--" in body
            or re.fullmatch(
                r"/agentd approve(?: " + re.escape(sha) + r")?", body.strip()
            )
            is None
        ):
            continue
        try:
            if (
                comment["created_at"] == comment["updated_at"]
                and _utc(comment["created_at"]) > activated
            ):
                return True
        except (ValueError, TypeError, KeyError):
            continue
    return False


def bootstrap_checks(
    checks: list[dict[str, Any]], required_checks: list[str], sha: str
) -> bool:
    """Require the latest GitHub Actions result for every configured exact-head job."""
    if not required_checks:
        return False
    latest = {}
    for check in sorted(checks, key=lambda row: row.get("id", 0)):
        if (check.get("app") or {}).get("slug") == "github-actions" and check.get(
            "name"
        ) in required_checks:
            latest[check["name"]] = check
    return all(
        latest.get(name, {}).get("status") == "completed"
        and latest.get(name, {}).get("conclusion") == "success"
        and latest.get(name, {}).get("head_sha") == sha
        for name in required_checks
    )


def approved_merge(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    *,
    actors: frozenset[str],
    sha: str,
    actor_ids: Mapping[str, int] | None = None,
) -> bool:
    """Accept an explicit maintainer merge or current-head maintainer approval."""
    if (
        pr.get("merged_at") is None
        or pr.get("merge_commit_sha") != sha
        or (pr.get("base") or {}).get("ref") != "master"
        or ((pr.get("base") or {}).get("repo") or {}).get("full_name")
        != "benednied/agentd"
        or (
            actor_ids is not None
            and ((pr.get("base") or {}).get("repo") or {}).get("id") != 1328873039
        )
    ):
        return False

    def trusted(actor: dict[str, Any]) -> bool:
        login = actor.get("login")
        return login in actors and (
            actor_ids is None or actor_ids.get(login) == actor.get("id")
        )

    if trusted(pr.get("merged_by") or {}):
        return True
    decisions = {}
    for review in sorted(
        reviews, key=lambda row: (row.get("submitted_at") or "", row.get("id", 0))
    ):
        actor = (review.get("user") or {}).get("login")
        if trusted(review.get("user") or {}) and review.get("state") in {
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
        }:
            decisions[actor] = review
    head = (pr.get("head") or {}).get("sha")
    return any(
        review.get("state") == "APPROVED" and review.get("commit_id") == head
        for review in decisions.values()
    )


# Capture before coding-current can be switched by this process.
_LOADED_RELEASE = Path(__file__).resolve().parents[1]


class ReleaseWatcher:
    def __init__(self, config: dict[str, Any]) -> None:
        if config.get("repository_name", "benednied/agentd") != "benednied/agentd":
            raise ValueError("Self-host watcher is bound to benednied/agentd")
        self.config = config
        self.source = Path(config["source_repository"]).resolve()
        self.root = Path(config["release_root"]).resolve()
        self.status_file = Path(config["status_file"]).resolve()
        self.baseline = config.get("baseline_commit", BASELINE)
        self.actors = frozenset(config["approved_actors"])
        self.actor_ids = config.get("approved_actor_ids")
        if self.actor_ids is not None and (
            not isinstance(self.actor_ids, dict)
            or not self.actor_ids
            or any(
                not isinstance(login, str)
                or not isinstance(identity, int)
                or isinstance(identity, bool)
                or identity <= 0
                for login, identity in self.actor_ids.items()
            )
        ):
            raise ValueError("Release approved actors require immutable positive IDs")
        self.image_repository = config.get("image_repository", "agentd-selfhost")
        if (
            _SHA.fullmatch(self.baseline) is None
            or not self.actors
            or re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", self.image_repository) is None
        ):
            raise ValueError(
                "Release watcher requires valid baseline, actors, and image"
            )
        self.bootstrap = config.get("bootstrap_pr")
        if self.bootstrap is not None:
            bootstrap = self.bootstrap
            if (
                not isinstance(bootstrap, dict)
                or not isinstance(bootstrap.get("number"), int)
                or isinstance(bootstrap.get("number"), bool)
                or bootstrap["number"] <= 0
                or not isinstance(bootstrap.get("head_commit"), str)
                or _SHA.fullmatch(bootstrap["head_commit"]) is None
                or not isinstance(bootstrap.get("activated_at"), str)
                or not isinstance(bootstrap.get("required_checks"), list)
                or not bootstrap["required_checks"]
                or any(
                    not isinstance(name, str) or not name.strip()
                    for name in bootstrap["required_checks"]
                )
                or self.actor_ids is None
            ):
                raise ValueError(
                    "Bootstrap requires exact head, checks, UTC and actor IDs"
                )
            _utc(bootstrap["activated_at"])
        self.status: dict[str, Any] = (
            json.loads(self.status_file.read_text())
            if self.status_file.is_file()
            else {"baseline_commit": self.baseline, "deployed_commit": self.baseline}
        )
        if self.status.get("baseline_commit") != self.baseline:
            raise ValueError("Established release watcher baseline cannot change")
        self._env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path(config.get("host_home", "/home/bened"))),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GH_CONFIG_DIR": str(Path(config["gh_config_dir"]).resolve()),
            "GH_PROMPT_DISABLED": "1",
            "AGENTD_PROFILE": "selfhost",
            "AGENTD_AUTOMATIC_ACTIVATION": "1",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
        self.abandon = None
        if config.get("abandon_controls") is not None:
            from host_abandon import HostAbandon

            self.abandon = HostAbandon(self, config["abandon_controls"])

    def record(self, stage: str, **values: Any) -> None:
        if stage != "blocked" and "error" not in values:
            values["error"] = None
        self.status.update(values, stage=stage, observed_at=utc_timestamp())
        write_json(self.status_file, self.status)
        if self.config.get("status_issue_number"):
            try:
                self.report_status()
                self.status.pop("github_status_error", None)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ReleaseBlocked,
                subprocess.TimeoutExpired,
            ) as error:
                # A lost status response cannot erase a successful activation.
                # The next meaningful observation reconciles the same marker.
                self.status["github_status_error"] = (
                    str(error)
                    if isinstance(error, ReleaseBlocked)
                    else type(error).__name__
                )
            write_json(self.status_file, self.status)

    def command(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = 120,
        cwd: Path | None = None,
        input_json: dict[str, Any] | None = None,
    ) -> str:
        process = subprocess.run(
            argv,
            cwd=cwd,
            env=self._env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=json.dumps(input_json) if input_json is not None else None,
        )
        if process.returncode:
            # Never persist raw host output: GitHub/Docker may print auth data.
            raise ReleaseBlocked("command_failed:" + Path(argv[0]).name)
        return process.stdout

    def git(self, *args: str) -> str:
        return self.command(("/usr/bin/git", "-C", str(self.source), *args)).strip()

    def github(self, endpoint: str, *, paginated: bool = False) -> Any:
        gh = str(self.config.get("gh_executable", "/usr/bin/gh"))
        argv = (gh, "api", "--hostname", "github.com")
        if paginated:
            argv += ("--paginate",)
        output = self.command((*argv, endpoint))
        if paginated:
            return pages(output)
        parsed = json.loads(output)
        if not isinstance(parsed, dict):
            raise ReleaseBlocked("github_response_invalid")
        return parsed

    def status_api(
        self,
        endpoint: str,
        *,
        data: dict[str, Any] | None = None,
        paginated: bool = False,
        method: str = "GET",
    ) -> Any:
        executable = str(
            self.config.get(
                "status_gh_executable", self.config.get("gh_executable", "/usr/bin/gh")
            )
        )
        argv = (executable, "api", "--hostname", "github.com", "--method", method)
        if paginated:
            argv += ("--paginate",)
        if data is not None:
            argv += ("--input", "-")
        output = self.command((*argv, endpoint), input_json=data)
        return pages(output) if paginated else json.loads(output)

    def report_status(self) -> None:
        number = self.config["status_issue_number"]
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("Release status requires a positive issue number")
        summary = {
            key: self.status.get(key)
            for key in ("stage", "deployed_commit", "candidate_commit", "error")
        }
        if self.status.get("bootstrap_head"):
            summary["bootstrap_head"] = self.status["bootstrap_head"]
        if self.status.get("abandon"):
            summary["abandon"] = self.status["abandon"]
        runtime = self.status.get("runtime_health", {})
        if runtime:
            summary["runtime_health"] = {
                "problems": runtime.get("reported_problems", []),
                "expected_waits": runtime.get("expected_waits", []),
            }
        fingerprint = hashlib.sha256(
            json.dumps(summary, sort_keys=True).encode()
        ).hexdigest()
        if self.status.get("github_status_fingerprint") == fingerprint:
            return
        marker = "<!-- agentd:selfhost-release-status:v1 -->"
        body = (
            marker
            + "\nAgentd release supervisor\n\n"
            + "\n".join(
                f"- {key.replace('_', ' ')}: `{value}`"
                for key, value in summary.items()
                if value is not None
            )
            + "\n\nBuilds require reviewed master commits, sandbox and package checks, "
            "database backups, and idle worker ownership. "
            "Retained databases are never reverted."
        )
        account = self.status_api("user")
        identity = account.get("id")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise ReleaseBlocked("github_status_identity_invalid")
        comments = self.status_api(
            f"repos/benednied/agentd/issues/{number}/comments", paginated=True
        )
        matching = [
            comment
            for comment in comments
            if (comment.get("user") or {}).get("id") == identity
            and isinstance(comment.get("body"), str)
            and comment["body"].startswith(marker + "\n")
        ]
        recorded = self.status.get("github_comment_id")
        selected = next((row for row in matching if row.get("id") == recorded), None)
        if selected is None:
            if len(matching) > 1:
                raise ReleaseBlocked("github_status_comment_ambiguous")
            selected = matching[0] if matching else None
        if selected is not None:
            if selected["body"] != body:
                response = self.status_api(
                    f"repos/benednied/agentd/issues/comments/{selected['id']}",
                    data={"body": body},
                    method="PATCH",
                )
            else:
                response = selected
        else:
            response = self.status_api(
                f"repos/benednied/agentd/issues/{number}/comments",
                data={"body": body},
                method="POST",
            )
        comment_id = response.get("id")
        if (
            not isinstance(comment_id, int)
            or isinstance(comment_id, bool)
            or comment_id <= 0
        ):
            raise ReleaseBlocked("github_status_comment_identity_invalid")
        self.status["github_comment_id"] = comment_id
        self.status["github_status_fingerprint"] = fingerprint

    def approved(self, sha: str) -> bool:
        prs = self.github(f"repos/benednied/agentd/commits/{sha}/pulls", paginated=True)
        for item in prs:
            number = item.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                raise ReleaseBlocked("github_pull_identity_invalid")
            pr = self.github(f"repos/benednied/agentd/pulls/{number}")
            reviews = self.github(
                f"repos/benednied/agentd/pulls/{number}/reviews", paginated=True
            )
            if approved_merge(
                pr, reviews, actors=self.actors, sha=sha, actor_ids=self.actor_ids
            ):
                return True
        return False

    def require_bootstrap_evidence(self, sha: str) -> None:
        """Bind approval to qualified source and the three deployed service images."""
        current = self.root.parent / "coding-current"
        image = self.image_repository + ":" + sha
        try:
            configured_images = [
                line
                for line in (current / "release.env").read_text().splitlines()
                if line.startswith("AGENTD_IMAGE=")
            ]
            evidence = json.loads((current / "package-qualified.json").read_text())
        except (OSError, ValueError):
            raise ReleaseBlocked("bootstrap_source_qualification_missing") from None
        if configured_images != ["AGENTD_IMAGE=" + image]:
            raise ReleaseBlocked("bootstrap_deployed_image_mismatch")
        if not isinstance(evidence, dict) or not isinstance(
            evidence.get("package"), dict
        ):
            raise ReleaseBlocked("bootstrap_source_qualification_missing")
        package = evidence["package"]
        if (
            evidence.get("commit") != sha
            or package.get("commit") != sha
            or package.get("passed") is not True
            or self.git("rev-parse", sha + "^{commit}") != sha
        ):
            raise ReleaseBlocked("bootstrap_source_qualification_missing")
        inspected = json.loads(
            self.command(("/usr/bin/docker", "image", "inspect", image), timeout=30)
        )
        if (
            not isinstance(inspected, list)
            or len(inspected) != 1
            or not isinstance(inspected[0], dict)
        ):
            raise ReleaseBlocked("bootstrap_image_inspection_invalid")
        image_info = inspected[0]
        digest = image_info.get("Id")
        labels = (image_info.get("Config") or {}).get("Labels") or {}
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or evidence.get("image_digest") != digest
            or labels.get("org.opencontainers.image.revision") != sha
        ):
            raise ReleaseBlocked("bootstrap_image_qualification_mismatch")
        containers = self.command(
            (
                str(current / "deploy/scripts/coding-compose.sh"),
                "ps",
                "--quiet",
                "coding-controller",
                "coding-worker",
                "coding-publisher",
            ),
            timeout=30,
        ).splitlines()
        if len(set(containers)) != 3 or any(
            re.fullmatch(r"[0-9a-f]{12,64}", container) is None
            for container in containers
        ):
            raise ReleaseBlocked("bootstrap_services_not_deployed")
        running = json.loads(
            self.command(("/usr/bin/docker", "inspect", *containers), timeout=30)
        )
        if (
            not isinstance(running, list)
            or len(running) != 3
            or any(
                not isinstance(row, dict)
                or row.get("Image") != digest
                or (row.get("State") or {}).get("Running") is not True
                for row in running
            )
        ):
            raise ReleaseBlocked("bootstrap_running_image_mismatch")

    def bootstrap_check_runs(self, sha: str) -> list[dict[str, Any]]:
        gh = str(self.config.get("gh_executable", "/usr/bin/gh"))
        return pages(
            self.command(
                (
                    gh,
                    "api",
                    "--hostname",
                    "github.com",
                    "--paginate",
                    "--jq",
                    ".check_runs",
                    f"repos/benednied/agentd/commits/{sha}/check-runs?filter=all&per_page=100",
                )
            )
        )

    def _bootstrap_pull(self) -> dict[str, Any]:
        bootstrap = self.bootstrap
        assert bootstrap is not None
        pr = self.github(f"repos/benednied/agentd/pulls/{bootstrap['number']}")
        base = pr.get("base") or {}
        repo = base.get("repo") or {}
        if (
            pr.get("number") != bootstrap["number"]
            or base.get("ref") != "master"
            or repo.get("full_name") != "benednied/agentd"
            or repo.get("id") != 1328873039
            or (pr.get("head") or {}).get("sha") != bootstrap["head_commit"]
        ):
            raise ReleaseBlocked("bootstrap_pull_identity_or_head_changed")
        return pr

    def _bootstrap_ready(self, pr: dict[str, Any]) -> bool:
        bootstrap = self.bootstrap
        assert bootstrap is not None and self.actor_ids is not None
        sha, number = bootstrap["head_commit"], bootstrap["number"]
        self.require_bootstrap_evidence(sha)
        reviews = self.github(
            f"repos/benednied/agentd/pulls/{number}/reviews", paginated=True
        )
        comments = self.github(
            f"repos/benednied/agentd/issues/{number}/comments", paginated=True
        )
        if not bootstrap_approval(
            pr,
            reviews,
            comments,
            sha=sha,
            activated_at=bootstrap["activated_at"],
            actors=self.actors,
            actor_ids=self.actor_ids,
        ):
            self.record(
                "bootstrap_waiting_approval", bootstrap_head=sha, candidate_commit=sha
            )
            return False
        if not bootstrap_checks(
            self.bootstrap_check_runs(sha), bootstrap["required_checks"], sha
        ):
            self.record(
                "bootstrap_waiting_checks", bootstrap_head=sha, candidate_commit=sha
            )
            return False
        return True

    def reconcile_bootstrap(self) -> bool:
        """Merge the bootstrap after fresh approval, checks and deployment proof."""
        if self.bootstrap is None:
            return False
        bootstrap = self.bootstrap
        sha, number = bootstrap["head_commit"], bootstrap["number"]
        pr = self._bootstrap_pull()
        if pr.get("merged_at") is not None:
            merge_sha = pr.get("merge_commit_sha")
            if not isinstance(merge_sha, str) or _SHA.fullmatch(merge_sha) is None:
                raise ReleaseBlocked("bootstrap_merged_commit_invalid")
            self.status.update(bootstrap_head=sha, bootstrap_merge_commit=merge_sha)
            write_json(self.status_file, self.status)
            return False
        if pr.get("state") != "open":
            raise ReleaseBlocked("bootstrap_pull_closed_without_merge")
        if not self._bootstrap_ready(pr):
            return True
        if pr.get("draft") is True:
            node = pr.get("node_id")
            if not isinstance(node, str) or not node:
                raise ReleaseBlocked("bootstrap_pull_node_invalid")
            # A successful mutation with a lost response is proved by GET.
            with suppress(
                OSError, ValueError, ReleaseBlocked, subprocess.TimeoutExpired
            ):
                self.status_api(
                    "graphql",
                    data={
                        "query": (
                            "mutation($id:ID!){markPullRequestReadyForReview("
                            "input:{pullRequestId:$id}){pullRequest{isDraft}}}"
                        ),
                        "variables": {"id": node},
                    },
                    method="POST",
                )
            pr = self._bootstrap_pull()
            if pr.get("draft") is not False:
                raise ReleaseBlocked("bootstrap_ready_response_unconfirmed")
            if not self._bootstrap_ready(pr):
                return True
        # Re-read the exact head before the CAS write. GitHub enforces sha atomically.
        pr = self._bootstrap_pull()
        if not self._bootstrap_ready(pr):
            return True
        self.record("bootstrap_merging", bootstrap_head=sha, candidate_commit=sha)
        with suppress(OSError, ValueError, ReleaseBlocked, subprocess.TimeoutExpired):
            self.status_api(
                f"repos/benednied/agentd/pulls/{number}/merge",
                data={"sha": sha, "merge_method": "merge"},
                method="PUT",
            )
        # GET is authoritative after success, timeout, or lost merge response.
        merged = self._bootstrap_pull()
        merge_sha = merged.get("merge_commit_sha")
        if (
            merged.get("merged_at") is None
            or not isinstance(merge_sha, str)
            or _SHA.fullmatch(merge_sha) is None
        ):
            raise ReleaseBlocked("bootstrap_merge_response_unconfirmed")
        self.record(
            "bootstrap_merged", bootstrap_head=sha, bootstrap_merge_commit=merge_sha
        )
        return False

    def require_profile(self, sha: str) -> None:
        files = frozenset(self.git("ls-tree", "-r", "--name-only", sha).splitlines())
        if not _REQUIRED.issubset(files):
            raise ReleaseBlocked("merged_release_lacks_standing_selfhost_profile")
        script = self.git("show", sha + ":deploy/scripts/coding-release.sh")
        if "AGENTD_AUTOMATIC_ACTIVATION" not in script or "selfhost)" not in script:
            raise ReleaseBlocked("merged_release_lacks_safe_automatic_activation")

    def inspect_health(self) -> None:
        """Observe services without changing worker ownership or running models."""
        current = self.root.parent / "coding-current"
        problems, waits = [], []
        try:
            process = subprocess.run(
                (
                    str(current / "deploy/scripts/coding-compose.sh"),
                    "exec",
                    "-T",
                    "coding-controller",
                    "agentd",
                    "github",
                    "--config",
                    "/etc/agentd/controller.json",
                    "health",
                    "--liveness",
                    "--role",
                    "controller",
                ),
                env=self._env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            # The health CLI returns 1 with valid JSON when liveness fails.
            report = json.loads(process.stdout)
            liveness = report["liveness"]
            if not isinstance(liveness, dict):
                raise ValueError("Runtime health liveness must be an object")
            for role in ("controller", "publisher"):
                pulse = liveness[role]
                if not isinstance(pulse, dict):
                    raise ValueError("Runtime health pulse must be an object")
                if pulse.get("live") is not True:
                    problems.append(role + "_polling_stale")
                if pulse.get("state") == "ownership_blocked":
                    problems.append("provider_ownership_unresolved")
            if report.get("source_fresh") is not True:
                problems.append("source_polling_stale")
            workers = report.get("workers")
            if workers is not None and (
                not isinstance(workers, list)
                or any(not isinstance(worker, dict) for worker in workers)
            ):
                raise ValueError("Runtime health workers must be a list")
            if not workers or any(
                worker.get("fresh") is not True for worker in workers
            ):
                problems.append("worker_heartbeat_stale")
            provider = report.get("provider_wait_reason")
            if provider in {"quota_unknown", "quota_stale"}:
                problems.append("provider_telemetry_unavailable")
            elif provider:
                waits.append("quota")
            if report.get("draining"):
                waits.append("drained")
            # Active run IDs also appear in unresolved_runs during normal work.
            # Only the explicit ownership_blocked pulse denotes an outage.
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
            problems = ["runtime_health_read_failed"]
        failures = (
            int(self.status.get("health_failure_observations", 0)) + 1
            if problems
            else 0
        )
        persistent = failures >= int(self.config.get("health_failure_observations", 2))
        self.status["health_failure_observations"] = failures
        self.status["runtime_health"] = {
            "problems": sorted(set(problems)),
            "reported_problems": sorted(set(problems)) if persistent else [],
            "expected_waits": sorted(set(waits)),
            "observed_at": utc_timestamp(),
        }
        write_json(self.status_file, self.status)

    def acknowledge_supervisor_start(self) -> None:
        """Only the process loaded from the activated release clears restart intent.

        systemctl can kill its caller before returning. Clearing the intent after
        that call races with SIGTERM; clearing it before loses crash recovery.
        The replacement process acknowledges under the supervisor ownership lock.
        """
        if not self.status.get("restart_pending"):
            return
        sha = self.status.get("deployed_commit")
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            return
        expected = self.root / sha
        current = self.root.parent / "coding-current"
        if expected != _LOADED_RELEASE or current.resolve() != expected:
            return
        self.status["restart_pending"] = False
        self.record("current", supervisor_commit=sha)

    def restart_supervisor(self) -> None:
        if not self.status.get("restart_pending"):
            return
        self.command(
            (
                "/usr/bin/systemctl",
                "--user",
                "try-restart",
                "--no-block",
                "agentd-selfhost-release.service",
            ),
            timeout=30,
        )

    def backup(self, sha: str) -> list[str]:
        paths = []
        directory = self.status_file.parent / "release-backups" / sha
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for index, raw in enumerate(self.config.get("databases", ())):
            source = Path(raw).absolute()
            if not source.is_file() or source.is_symlink():
                raise ReleaseBlocked("required_database_missing")
            destination = directory / f"state-{index}.sqlite"
            if not destination.exists():
                with (
                    closing(
                        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                    ) as old,
                    closing(sqlite3.connect(destination)) as snapshot,
                ):
                    old.backup(snapshot)
                    if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ReleaseBlocked("database_backup_invalid")
                destination.chmod(0o600)
            with closing(sqlite3.connect(destination)) as snapshot:
                if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ReleaseBlocked("database_backup_invalid")
            paths.append(str(destination))
        return paths

    def prepare(self, sha: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        release = self.root / sha
        image = self.image_repository + ":" + sha
        evidence_file = release / "package-qualified.json"
        if evidence_file.is_file():
            evidence = json.loads(evidence_file.read_text())
            digest = self.command(
                ("/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", image)
            ).strip()
            if evidence.get("commit") == sha and evidence.get("image_digest") == digest:
                return release
            raise ReleaseBlocked("prepared_release_evidence_changed")
        if release.exists():
            raise ReleaseBlocked("partial_release_requires_new_preparation")
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.root) as temp:
            staging = Path(temp) / "release"
            staging.mkdir()
            archive = Path(temp) / "source.tar"
            self.git("archive", "--format=tar", "--output=" + str(archive), sha)
            with tarfile.open(archive) as exported:
                exported.extractall(staging, filter="data")
            (staging / "release.env").write_text(
                f"AGENTD_IMAGE={image}\nAGENTD_CODING_COMPOSE_PROJECT=agentd-selfhost-coding\n"
            )
            (staging / "release.env").chmod(0o600)
            self.record("building", candidate_commit=sha)
            self.command(
                (
                    "/usr/bin/docker",
                    "build",
                    "--pull=false",
                    "--build-arg",
                    "SOURCE_SHA=" + sha,
                    "--tag",
                    image,
                    str(staging),
                ),
                timeout=3600,
            )
            verification = Path(temp) / "verification"
            self.command(
                (
                    "/usr/bin/git",
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--",
                    str(self.source),
                    str(verification),
                )
            )
            self.command(
                (
                    "/usr/bin/git",
                    "-C",
                    str(verification),
                    "fetch",
                    "--no-tags",
                    "origin",
                    sha,
                )
            )
            self.command(
                ("/usr/bin/git", "-C", str(verification), "checkout", "--detach", sha)
            )
            self.record("qualifying", candidate_commit=sha)
            command = (
                "/usr/bin/docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "1000:1000",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "512",
                "--memory",
                "12g",
                "--cpus",
                "6",
                "--security-opt",
                "no-new-privileges:true",
                "--security-opt",
                "apparmor=lxc-usernsexec",
                "--security-opt",
                "seccomp=" + str(staging / "deploy/container/seccomp-agentd.json"),
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=1073741824,uid=1000,gid=1000,mode=1770",
                "--mount",
                "type=bind,src=" + str(verification) + ",dst=/candidate",
                "--entrypoint",
                "/opt/agentd/venv/bin/python",
                image,
                "-I",
                "-c",
                _PACKAGE_GATE,
                sha,
            )
            output = self.command(command, timeout=660)
            lines = [line for line in output.splitlines() if line.strip()]
            package = json.loads(lines[-1])
            if package.get("passed") is not True or package.get("commit") != sha:
                raise ReleaseBlocked("package_qualification_failed")
            digest = self.command(
                ("/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", image)
            ).strip()
            write_json(
                staging / "package-qualified.json",
                {
                    "commit": sha,
                    "image_digest": digest,
                    "package": package,
                    "observed_at": utc_timestamp(),
                },
            )
            staging.rename(release)
        return release

    def tick(self) -> None:
        self.inspect_health()
        if self.abandon is not None:
            from host_abandon import AbandonBlocked

            try:
                pending = self.abandon.tick()
            except AbandonBlocked as error:
                raise ReleaseBlocked(str(error)) from None
            if pending:
                self.record(str(self.status.get("stage", "abandon_pending")))
                return
        if self.reconcile_bootstrap():
            return
        origin = self.git("remote", "get-url", "origin")
        if origin != "https://github.com/benednied/agentd.git":
            raise ReleaseBlocked("source_origin_changed")
        self.git("fetch", "--no-tags", "origin", "master")
        sha = self.git("rev-parse", "refs/remotes/origin/master")
        if _SHA.fullmatch(sha) is None:
            raise ReleaseBlocked("master_commit_invalid")
        previous = str(self.status["deployed_commit"])
        if sha == previous:
            self.restart_supervisor()
            self.record("current", candidate_commit=sha)
            return
        self.git("merge-base", "--is-ancestor", previous, sha)
        self.require_profile(sha)
        commits = self.git(
            "rev-list", "--first-parent", "--reverse", previous + ".." + sha
        ).splitlines()
        for commit in commits:
            if not self.approved(commit):
                raise ReleaseBlocked("master_commit_missing_maintainer_approval")
        release = self.prepare(sha)
        backups = self.backup(sha)
        self.record("activating", candidate_commit=sha, backups=backups)
        self.command(
            (
                str(release / "deploy/scripts/coding-release.sh"),
                "activate",
                str(release),
            ),
            timeout=600,
        )
        self.record(
            "current",
            deployed_commit=sha,
            candidate_commit=sha,
            release=str(release),
            error=None,
            restart_pending=True,
        )
        self.restart_supervisor()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    watcher = ReleaseWatcher(json.loads(args.config.read_text()))
    watcher.status_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = False

    def request_stop(_signal: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with watcher.status_file.with_suffix(".lock").open("a+") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        watcher.acknowledge_supervisor_start()
        while not stop:
            try:
                watcher.tick()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                sqlite3.Error,
                ReleaseBlocked,
                subprocess.TimeoutExpired,
            ) as error:
                watcher.record(
                    "blocked",
                    error=str(error)
                    if isinstance(error, ReleaseBlocked)
                    else type(error).__name__,
                )
                if args.once:
                    return 1
            if args.once:
                return 0
            deadline = time.monotonic() + float(watcher.config.get("poll_seconds", 300))
            while not stop and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
