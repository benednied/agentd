import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from watch_selfhost_release import (
    _PACKAGE_GATE,
    _REQUIRED,
    BASELINE,
    ReleaseBlocked,
    ReleaseWatcher,
    approved_merge,
    pages,
)


def config(root):
    return {
        "source_repository": str(root / "source"),
        "release_root": str(root / "releases"),
        "status_file": str(root / "state/release-status.json"),
        "gh_config_dir": str(root / "github-read"),
        "approved_actors": ["maintainer"],
    }


def pull(**changes):
    return {
        "merged_at": "2026-10-03T12:00:00Z",
        "merge_commit_sha": "a" * 40,
        "base": {"ref": "master", "repo": {"full_name": "benednied/agentd"}},
        "head": {"sha": "b" * 40},
        "merged_by": {"login": "maintainer"},
        **changes,
    }


def test_only_merged_master_prs_with_maintainer_authority_are_releases():
    actors = frozenset({"maintainer"})
    assert approved_merge(pull(), [], actors=actors, sha="a" * 40)
    assert not approved_merge(pull(merged_at=None), [], actors=actors, sha="a" * 40)
    assert not approved_merge(pull(), [], actors=actors, sha="c" * 40)
    assert not approved_merge(
        pull(merged_by={"login": "outsider"}), [], actors=actors, sha="a" * 40
    )
    assert not approved_merge(
        pull(base={"ref": "other"}), [], actors=actors, sha="a" * 40
    )


def test_current_head_review_can_authorize_bot_merge_but_revocation_wins():
    pr = pull(merged_by={"login": "merge-bot"})
    actors = frozenset({"maintainer"})
    approval = {
        "id": 1,
        "user": {"login": "maintainer"},
        "state": "APPROVED",
        "commit_id": "b" * 40,
        "submitted_at": "2026-10-03T10:00:00Z",
    }
    assert approved_merge(pr, [approval], actors=actors, sha="a" * 40)
    assert not approved_merge(
        pr, [{**approval, "commit_id": "c" * 40}], actors=actors, sha="a" * 40
    )
    revoked = {**approval, "id": 2, "state": "CHANGES_REQUESTED"}
    assert not approved_merge(pr, [approval, revoked], actors=actors, sha="a" * 40)


def test_identity_bound_release_requires_actor_and_immutable_repository_ids():
    actors = frozenset({"maintainer"})
    identities = {"maintainer": 7}
    approved = pull(
        base={
            "ref": "master",
            "repo": {"full_name": "benednied/agentd", "id": 1328873039},
        },
        merged_by={"login": "maintainer", "id": 7},
    )
    assert approved_merge(
        approved, [], actors=actors, sha="a" * 40, actor_ids=identities
    )
    wrong_actor = {**approved, "merged_by": {"login": "maintainer", "id": 8}}
    assert not approved_merge(
        wrong_actor, [], actors=actors, sha="a" * 40, actor_ids=identities
    )
    wrong_repository = {
        **approved,
        "base": {"ref": "master", "repo": {"full_name": "benednied/agentd", "id": 1}},
    }
    assert not approved_merge(
        wrong_repository, [], actors=actors, sha="a" * 40, actor_ids=identities
    )
    bot_merged = {**approved, "merged_by": {"login": "bot", "id": 99}}
    review = {
        "user": {"login": "maintainer", "id": 7},
        "state": "APPROVED",
        "commit_id": "b" * 40,
    }
    assert approved_merge(
        bot_merged, [review], actors=actors, sha="a" * 40, actor_ids=identities
    )
    review["user"]["id"] = 8
    assert not approved_merge(
        bot_merged, [review], actors=actors, sha="a" * 40, actor_ids=identities
    )


def test_github_pagination_requires_complete_page_lists():
    assert pages(json.dumps([[{"number": 1}], [{"number": 2}]])) == [
        {"number": 1},
        {"number": 2},
    ]
    assert pages('[{"number":1}]\n[{"number":2}]') == [{"number": 1}, {"number": 2}]
    with pytest.raises(json.JSONDecodeError):
        pages('[{"number":1}]\n[partial')
    with pytest.raises(ReleaseBlocked):
        pages(json.dumps({"number": 1}))


def test_baseline_skips_existing_master_and_never_replaces_bootstrap(tmp_path):
    watcher = ReleaseWatcher(config(tmp_path))
    calls = []

    def git(*argv):
        calls.append(argv)
        if argv[:2] == ("remote", "get-url"):
            return "https://github.com/benednied/agentd.git"
        if argv[0] == "rev-parse":
            return BASELINE
        return ""

    watcher.git = git
    watcher.prepare = lambda _sha: pytest.fail("existing master must not be activated")
    watcher.tick()
    status = json.loads(watcher.status_file.read_text())
    assert status["deployed_commit"] == BASELINE
    assert status["stage"] == "current"
    assert not any(call[0] == "merge-base" for call in calls)


def test_old_master_without_standing_profile_is_rejected(tmp_path):
    watcher = ReleaseWatcher(config(tmp_path))
    watcher.git = lambda *_: "Dockerfile\ndeploy/scripts/coding-release.sh"
    with pytest.raises(ReleaseBlocked, match="standing_selfhost_profile"):
        watcher.require_profile("a" * 40)


def test_activation_follows_review_package_and_backup_gates(tmp_path):
    watcher = ReleaseWatcher(config(tmp_path))
    events = []

    def git(*argv):
        if argv[:2] == ("remote", "get-url"):
            return "https://github.com/benednied/agentd.git"
        if argv[0] == "rev-parse":
            return "a" * 40
        if argv[0] == "rev-list":
            return "a" * 40
        events.append(argv[0])
        return ""

    watcher.git = git
    watcher.require_profile = lambda _sha: events.append("profile")
    watcher.approved = lambda _sha: events.append("approved") or True
    watcher.prepare = lambda _sha: events.append("qualified") or tmp_path / "release"
    watcher.backup = lambda _sha: events.append("backup") or ["snapshot"]
    watcher.command = lambda *_args, **_kwargs: events.append("activate") or ""
    watcher.tick()
    assert events.index("approved") < events.index("qualified")
    assert events.index("qualified") < events.index("backup") < events.index("activate")
    assert watcher.status["deployed_commit"] == "a" * 40
    assert watcher._env["AGENTD_PROFILE"] == "selfhost"
    assert watcher._env["AGENTD_AUTOMATIC_ACTIVATION"] == "1"
    assert "GH_TOKEN" not in watcher._env and "CODEX_HOME" not in watcher._env


def test_activation_failure_retains_deployed_identity_and_databases(tmp_path):
    watcher = ReleaseWatcher(config(tmp_path))
    watcher.git = lambda *args: (
        "https://github.com/benednied/agentd.git"
        if args[0] == "remote"
        else "a" * 40
        if args[0] in {"rev-parse", "rev-list"}
        else ""
    )
    watcher.require_profile = lambda _sha: None
    watcher.approved = lambda _sha: True
    watcher.prepare = lambda _sha: tmp_path / "release"
    watcher.backup = lambda _sha: ["retained snapshot"]

    def command(*_args, **_kwargs):
        raise ReleaseBlocked("activation_failed")

    watcher.command = command
    with pytest.raises(ReleaseBlocked, match="activation_failed"):
        watcher.tick()
    assert watcher.status["deployed_commit"] == BASELINE
    assert watcher.status["backups"] == ["retained snapshot"]


def test_backup_is_consistent_and_never_restores_existing_database(tmp_path):
    source = tmp_path / "live.sqlite"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE retained(value INTEGER)")
        database.execute("INSERT INTO retained VALUES (7)")
    watcher = ReleaseWatcher({**config(tmp_path), "databases": [str(source)]})
    paths = watcher.backup("a" * 40)
    with sqlite3.connect(paths[0]) as backup:
        assert backup.execute("SELECT value FROM retained").fetchone() == (7,)
    with sqlite3.connect(source) as live:
        live.execute("UPDATE retained SET value=8")
    watcher.backup("a" * 40)
    with sqlite3.connect(source) as live:
        assert live.execute("SELECT value FROM retained").fetchone() == (8,)


def test_container_gate_has_real_containment_probe_and_package_command():
    assert "BubblewrapValidationRunner" in _PACKAGE_GATE
    assert "Git metadata writable" in _PACKAGE_GATE
    assert "External state readable" in _PACKAGE_GATE
    assert "connect_ex" in _PACKAGE_GATE
    assert "validate_python_package.py" in _PACKAGE_GATE
    assert "--expected-commit" in _PACKAGE_GATE
    assert "src/agentd/intake/workflow.py" in _REQUIRED


def test_release_status_reconciles_lost_post_without_duplicate_or_raw_output(tmp_path):
    watcher = ReleaseWatcher({**config(tmp_path), "status_issue_number": 90})
    comments, writes = [], []
    lose_response = [True]

    def api(endpoint, *, data=None, paginated=False, method="GET"):
        if endpoint == "user":
            return {"id": 7}
        if paginated:
            return list(comments)
        writes.append(method)
        if method == "POST":
            comment = {"id": 11, "user": {"id": 7}, "body": data["body"]}
            comments.append(comment)
            if lose_response[0]:
                lose_response[0] = False
                raise ReleaseBlocked("lost_status_response")
            return comment
        comments[0]["body"] = data["body"]
        return comments[0]

    watcher.status_api = api
    watcher.record("building", candidate_commit="a" * 40)
    assert watcher.status["github_status_error"] == "lost_status_response"
    watcher.record("building", candidate_commit="a" * 40)
    assert writes == ["POST"]
    assert watcher.status["github_comment_id"] == 11
    assert "github_status_error" not in watcher.status
    watcher.record("building", candidate_commit="a" * 40)
    assert writes == ["POST"]
    watcher.record("qualifying", candidate_commit="a" * 40)
    assert writes == ["POST", "PATCH"]
    assert str(tmp_path) not in comments[0]["body"]


def test_release_status_does_not_overwrite_another_authors_marker(tmp_path):
    watcher = ReleaseWatcher({**config(tmp_path), "status_issue_number": 90})
    writes = []

    def api(endpoint, *, data=None, paginated=False, method="GET"):
        if endpoint == "user":
            return {"id": 7}
        if paginated:
            return [
                {
                    "id": 2,
                    "user": {"id": 8},
                    "body": "<!-- agentd:selfhost-release-status:v1 -->\nforged",
                }
            ]
        writes.append(method)
        return {"id": 11}

    watcher.status_api = api
    watcher.record("current")
    assert writes == ["POST"]


def test_selfhost_release_unit_is_trusted_host_service_without_worker_socket():
    root = Path(__file__).parents[1]
    text = (root / "deploy/systemd/agentd-selfhost-release.service").read_text()
    assert "watch_selfhost_release.py" in text
    assert "NoNewPrivileges=yes" in text
    assert "ProtectHome=read-only" in text
    assert "coding-current" in text
    assert "XDG_RUNTIME_DIR=/run/user/1000" in text


def test_host_health_treats_quota_and_drain_as_waits_not_worker_outages(
    tmp_path, monkeypatch
):
    watcher = ReleaseWatcher(config(tmp_path))
    report = {
        "liveness": {
            "controller": {"live": True, "state": "polling"},
            "publisher": {"live": True, "state": "waiting_review"},
        },
        "source_fresh": True,
        "workers": [{"fresh": True, "active_runs": 1}],
        "provider_wait_reason": "quota_provider_pressure",
        "draining": True,
        "unresolved_runs": ["normally-running-job"],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(report), stderr=""
        ),
    )
    watcher.inspect_health()
    health = watcher.status["runtime_health"]
    assert health["problems"] == health["reported_problems"] == []
    assert health["expected_waits"] == ["drained", "quota"]


def test_host_health_reports_persistent_failure_and_baseline_does_not_erase_it(
    tmp_path, monkeypatch
):
    watcher = ReleaseWatcher(config(tmp_path))
    report = {
        "liveness": {
            "controller": {"live": False, "state": "ownership_blocked"},
            "publisher": {"live": False, "state": "polling"},
        },
        "source_fresh": False,
        "workers": [{"fresh": False, "active_runs": 1}],
        "provider_wait_reason": "quota_unknown",
        "draining": False,
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=json.dumps(report), stderr=""
        ),
    )
    watcher.inspect_health()
    assert watcher.status["runtime_health"]["reported_problems"] == []
    watcher.inspect_health()
    problems = watcher.status["runtime_health"]["reported_problems"]
    assert "provider_ownership_unresolved" in problems
    assert "controller_polling_stale" in problems
    assert "publisher_polling_stale" in problems
    assert "source_polling_stale" in problems
    watcher.record("current", candidate_commit=BASELINE)
    assert watcher.status["runtime_health"]["reported_problems"] == problems


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "tag",
        "package",
        "digest",
        "label",
        "running_image",
        "stopped",
        "missing_service",
    ],
)
def test_bootstrap_evidence_binds_qualified_source_to_every_running_service(
    tmp_path, broken
):
    watcher = ReleaseWatcher(config(tmp_path))
    sha, digest = "a" * 40, "sha256:" + "b" * 64
    current = tmp_path / "coding-current"
    current.mkdir()
    (current / "release.env").write_text(
        "AGENTD_IMAGE=agentd-selfhost:" + ("c" * 40 if broken == "tag" else sha) + "\n"
    )
    (current / "package-qualified.json").write_text(
        json.dumps(
            {
                "commit": sha,
                "image_digest": "sha256:" + "d" * 64 if broken == "digest" else digest,
                "package": {"commit": sha, "passed": broken != "package"},
            }
        )
    )
    watcher.git = lambda *_args: sha
    container_ids = [str(number) * 64 for number in (1, 2, 3)]

    def command(argv, **_kwargs):
        if argv[:3] == ("/usr/bin/docker", "image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": digest,
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": "c" * 40
                                if broken == "label"
                                else sha
                            }
                        },
                    }
                ]
            )
        if argv[:2] == ("/usr/bin/docker", "inspect"):
            return json.dumps(
                [
                    {
                        "Image": "sha256:" + "c" * 64
                        if broken == "running_image" and index == 1
                        else digest,
                        "State": {"Running": not (broken == "stopped" and index == 2)},
                    }
                    for index in range(3)
                ]
            )
        assert argv[1:] == (
            "ps",
            "--quiet",
            "coding-controller",
            "coding-worker",
            "coding-publisher",
        )
        return "\n".join(
            container_ids[:2] if broken == "missing_service" else container_ids
        )

    watcher.command = command
    if broken is None:
        watcher.require_bootstrap_evidence(sha)
    else:
        with pytest.raises(ReleaseBlocked):
            watcher.require_bootstrap_evidence(sha)


def test_replacement_acknowledges_restart_when_systemctl_kills_old_process(
    tmp_path, monkeypatch
):
    import watch_selfhost_release as module

    watcher = ReleaseWatcher(config(tmp_path))
    sha = "a" * 40
    release = watcher.root / sha
    release.mkdir(parents=True)
    (watcher.root.parent / "coding-current").symlink_to(release)
    watcher.record("current", deployed_commit=sha, restart_pending=True)

    def killed(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(watcher, "command", killed)
    with pytest.raises(SystemExit):
        watcher.restart_supervisor()
    assert json.loads(watcher.status_file.read_text())["restart_pending"] is True
    replacement = ReleaseWatcher(config(tmp_path))
    monkeypatch.setattr(module, "_LOADED_RELEASE", release)
    replacement.acknowledge_supervisor_start()
    assert replacement.status["restart_pending"] is False
    assert replacement.status["supervisor_commit"] == sha
    # No second self-restart, including after another process crash.
    restarted = ReleaseWatcher(config(tmp_path))
    monkeypatch.setattr(restarted, "command", killed)
    restarted.restart_supervisor()


def test_old_supervisor_cannot_acknowledge_new_release(tmp_path, monkeypatch):
    import watch_selfhost_release as module

    watcher = ReleaseWatcher(config(tmp_path))
    sha = "a" * 40
    release = watcher.root / sha
    release.mkdir(parents=True)
    (watcher.root.parent / "coding-current").symlink_to(release)
    watcher.record("current", deployed_commit=sha, restart_pending=True)
    monkeypatch.setattr(module, "_LOADED_RELEASE", watcher.root / ("b" * 40))
    watcher.acknowledge_supervisor_start()
    assert watcher.status["restart_pending"] is True
