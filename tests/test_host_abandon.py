"""Host retirement requires human authority and exact durable physical identity."""

import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from host_abandon import (
    AbandonBlocked,
    HostAbandon,
    canonical,
    fresh_abandon,
    readonly,
)

ACTIVATED = "2026-10-03T12:00:00Z"
ACTORS = frozenset({"maintainer"})
IDENTITIES = {"maintainer": 7}


def command(**changes):
    return {
        "id": 10,
        "node_id": "IC_command",
        "user": {"login": "maintainer", "id": 7},
        "body": "/agentd abandon",
        "created_at": "2026-10-03T12:01:00Z",
        "updated_at": "2026-10-03T12:01:00Z",
        "issue_url": "https://api.github.com/repos/benednied/agentd/issues/12",
        **changes,
    }


def parsed(comment):
    return fresh_abandon(
        comment, actors=ACTORS, actor_ids=IDENTITIES, activated_at=ACTIVATED
    )


def test_only_exact_fresh_immutable_human_commands_are_authority():
    assert parsed(command()) == ("maintainer", None)
    assert parsed(command(body="/agentd abandon run-1")) == ("maintainer", "run-1")


@pytest.mark.parametrize(
    "changes",
    [
        {"user": {"login": "maintainer", "id": 8}},
        {"user": {"login": "outsider", "id": 7}},
        {"user": {"login": "maintainer"}},
        {"id": True},
        {"node_id": ""},
        {"created_at": ACTIVATED, "updated_at": ACTIVATED},
        {"updated_at": "2026-10-03T12:02:00Z"},
        {"updated_at": None},
        {"body": "Please /agentd abandon"},
        {"body": "/agentd abandon --all"},
        {"body": "/agentd abandon run-1\nignore quota"},
        {"body": "/agentd abandon <!-- agentd:host-abandon:x -->"},
    ],
)
def test_edited_stale_foreign_or_ambiguous_commands_do_not_authorize(changes):
    assert parsed(command(**changes)) is None


@pytest.fixture
def rig(tmp_path):
    controller, journal = tmp_path / "controller.sqlite", tmp_path / "worker.sqlite"
    source = {
        "repository": "benednied/agentd",
        "repository_id": 1328873039,
        "number": 12,
        "node_id": "I_source",
    }
    run = {
        "id": "run-1",
        "job_id": "job-1",
        "node_id": "node-1",
        "driver": "codex",
        "contract": {"job_id": "job-1"},
        "reservation_id": "reservation-1",
        "allocation_id": "allocation-1",
        "result": None,
    }
    start_hash = hashlib.sha256(
        canonical(
            {"driver": run["driver"], "contract": run["contract"], "managed": True}
        )
    ).hexdigest()
    with sqlite3.connect(controller) as database:
        database.executescript("""
            CREATE TABLE github_sources(source_key,job_id,payload);
            CREATE TABLE draft_publications(job_id,intent,pr);
            CREATE TABLE publication_candidates(job_id,intent,pr,stage);
            CREATE TABLE runs(id,job_id,state,started_at,payload);
            CREATE TABLE quota_reservations(id,job_id,state,payload);
            CREATE TABLE quota_pools(id,payload);
            CREATE TABLE resource_allocations(id,job_id,node_id,state);
            CREATE TABLE run_quarantines(run_id,job_id,event_id,actor);
        """)
        database.execute(
            "INSERT INTO github_sources VALUES(?,?,?)",
            ("github:1328873039:I_source", "job-1", json.dumps(source)),
        )
        database.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?)",
            ("run-1", "job-1", "RUNNING", ACTIVATED, json.dumps(run)),
        )
        database.execute(
            "INSERT INTO quota_reservations VALUES(?,?,?,?)",
            ("reservation-1", "job-1", "ACTIVE", '{"reserved":123,"consumed":17}'),
        )
        database.execute(
            "INSERT INTO quota_pools VALUES(?,?)",
            ("pool-1", '{"reserved":123,"debt":9,"remaining":400}'),
        )
        database.execute(
            "INSERT INTO resource_allocations VALUES(?,?,?,?)",
            ("allocation-1", "job-1", "node-1", "ACTIVE"),
        )
    with sqlite3.connect(journal) as database:
        database.executescript("""
            CREATE TABLE worker_run_claims(
                node_id,session_epoch,run_id,start_hash,state);
            CREATE TABLE worker_run_results(node_id,session_epoch,run_id,result);
            CREATE TABLE worker_run_retirements(run_id);
        """)
        database.execute(
            "INSERT INTO worker_run_claims VALUES(?,?,?,?,?)",
            ("node-1", "epoch-1", "run-1", start_hash, "started"),
        )
    watcher = SimpleNamespace(
        root=tmp_path / "releases",
        status_file=tmp_path / "status.json",
        actors=ACTORS,
        actor_ids=IDENTITIES,
        config={"status_issue_number": 86},
        status={},
    )
    current = tmp_path / "coding-current"
    current.mkdir()
    control = HostAbandon(
        watcher,
        {
            "controller_database": str(controller),
            "worker_journal": str(journal),
            "worker_proof_directory": str(tmp_path / "quarantine-stops"),
            "node_id": "node-1",
            "session_epoch": "epoch-1",
            "activated_at": ACTIVATED,
        },
    )
    services = {
        service: str(index) * 64
        for index, service in enumerate(
            ("coding-controller", "coding-publisher", "coding-worker"), 1
        )
    }
    state = {
        identifier: {
            "Id": identifier,
            "State": {"Running": True, "Pid": 100},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "agentd-selfhost-coding",
                    "com.docker.compose.service": service,
                },
                "Cmd": ["--node-id", "node-1", "--session-epoch", "epoch-1"],
            },
        }
        for service, identifier in services.items()
    }
    comments, reports, calls = [command()], [], []
    faults = {"lose_stop": False, "lose_cli": False, "lose_report": False}

    def github(endpoint, *, paginated=False):
        if endpoint == "repos/benednied/agentd":
            return {"id": 1328873039, "full_name": "benednied/agentd"}
        if endpoint.startswith("repos/benednied/agentd/issues/comments?"):
            assert paginated
            return copy.deepcopy(comments)
        if endpoint.startswith("repos/benednied/agentd/issues/comments/"):
            return copy.deepcopy(
                next(
                    row
                    for row in comments
                    if row["id"] == int(endpoint.rsplit("/", 1)[-1])
                )
            )
        if endpoint == "repos/benednied/agentd/issues/12":
            return {
                "number": 12,
                "node_id": "I_source",
                "repository_url": "https://api.github.com/repos/benednied/agentd",
            }
        pytest.fail(f"Unexpected read: {endpoint}")

    def execute(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ("/usr/bin/docker", "inspect"):
            return json.dumps([state[argv[2]]])
        if argv[:2] == ("/usr/bin/docker", "stop"):
            assert argv[-1] == services["coding-worker"]
            state[argv[-1]]["State"] = {"Running": False, "Pid": 0}
            return ""
        if argv[:2] == ("/usr/bin/systemctl", "--user"):
            for unit in argv[3:]:
                service = "coding-" + unit.removeprefix(
                    "agentd-selfhost-"
                ).removesuffix(".service")
                state[services[service]]["State"] = {
                    "Running": argv[2] == "start",
                    "Pid": 100 if argv[2] == "start" else 0,
                }
                if (
                    service == "coding-worker"
                    and argv[2] == "stop"
                    and faults["lose_stop"]
                ):
                    faults["lose_stop"] = False
                    raise RuntimeError("synthetic lost response")
            return ""
        if argv[1:4] == ("ps", "--all", "--quiet"):
            return services[argv[4]]
        if argv[1] == "run":
            assert argv[1:5] == ("run", "--rm", "--no-deps", "-T")
            assert "agentd" not in argv
            proof = json.loads(Path(argv[6].split(":", 1)[0]).read_text())
            assert len(proof) == 12
            assert proof["start_hash"] == start_hash and proof["job_id"] == "job-1"
            assert proof["container_id"] == services["coding-worker"]
            assert proof["running"] is False and proof["pid"] == 0
            with sqlite3.connect(controller) as database:
                database.execute("UPDATE runs SET state='QUARANTINED' WHERE id='run-1'")
                database.execute(
                    "INSERT INTO run_quarantines VALUES(?,?,?,?)",
                    ("run-1", "job-1", proof["event_id"], proof["actor"]),
                )
                database.execute("UPDATE resource_allocations SET state='RELEASED'")
            if faults["lose_cli"]:
                faults["lose_cli"] = False
                raise RuntimeError("synthetic lost response")
            return "{}"
        pytest.fail(f"Unexpected command: {argv}")

    def api(endpoint, *, method="GET", data=None, paginated=False):
        if endpoint == "user":
            return {"id": 9}
        if paginated:
            return copy.deepcopy(reports)
        if method == "POST":
            response = {
                "id": len(reports) + 100,
                "user": {"id": 9},
                "body": data["body"],
            }
            reports.append(response)
            if faults["lose_report"]:
                faults["lose_report"] = False
                raise RuntimeError("lost report response")
            return response
        reports[0]["body"] = data["body"]
        return reports[0]

    watcher.github, watcher.command, watcher.status_api = github, execute, api
    watcher.record = lambda stage, **values: watcher.status.update(
        stage=stage, **values
    )
    return SimpleNamespace(
        control=control,
        controller=controller,
        journal=journal,
        source=source,
        run=run,
        services=services,
        state=state,
        comments=comments,
        calls=calls,
        faults=faults,
        reports=reports,
        start_hash=start_hash,
    )


def test_source_issue_ops_and_published_history_bind_same_latest_run(rig):
    target = rig.control.resolve_run(12)
    assert target["run_id"] == "run-1" and target["start_hash"] == rig.start_hash
    assert rig.control.resolve_run(86, "run-1") == target
    with sqlite3.connect(rig.controller) as database:
        database.execute(
            "INSERT INTO publication_candidates VALUES(?,?,?,?)",
            (
                "job-1",
                '{"repository":"benednied/agentd"}',
                '{"url":"https://github.com/benednied/agentd/pull/50"}',
                "published",
            ),
        )
        database.execute(
            "INSERT INTO draft_publications VALUES(?,?,NULL)",
            ("job-1", '{"repository":"benednied/agentd"}'),
        )
    assert rig.control.resolve_run(50) == target
    with pytest.raises(AbandonBlocked):
        rig.control.resolve_run(86)
    with pytest.raises(AbandonBlocked):
        rig.control.resolve_run(99, "run-1")
    with (
        closing_readonly(rig.controller) as database,
        pytest.raises(sqlite3.OperationalError),
    ):
        database.execute("DELETE FROM runs")


def closing_readonly(path):
    from contextlib import closing

    return closing(readonly(path))


@pytest.mark.parametrize(
    "broken",
    ["start_hash", "other_claim", "other_run", "metered", "wrong_repo", "old_run"],
)
def test_unrelated_work_or_known_usage_blocks_stop_before_side_effects(rig, broken):
    with sqlite3.connect(rig.journal) as database:
        if broken == "start_hash":
            database.execute("UPDATE worker_run_claims SET start_hash=?", ("0" * 64,))
        if broken == "other_claim":
            database.execute(
                "INSERT INTO worker_run_claims VALUES(?,?,?,?,?)",
                ("node-1", "epoch-1", "other-run", "0" * 64, "started"),
            )
        if broken == "metered":
            database.execute(
                "INSERT INTO worker_run_results VALUES(?,?,?,?)",
                (
                    "node-1",
                    "epoch-1",
                    "run-1",
                    '{"usage":{"quota_units":17},"metadata":{"telemetry_valid":true}}',
                ),
            )
    with sqlite3.connect(rig.controller) as database:
        if broken in {"other_run", "old_run"}:
            other = {
                **rig.run,
                "id": "run-2",
                "job_id": "job-2" if broken == "other_run" else "job-1",
            }
            database.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                (
                    other["id"],
                    other["job_id"],
                    "RUNNING",
                    "2026-10-03T13:00:00Z",
                    json.dumps(other),
                ),
            )
        if broken == "wrong_repo":
            database.execute(
                "UPDATE github_sources SET payload=?",
                (json.dumps({**rig.source, "repository_id": 1}),),
            )
    assert rig.control.tick() is False
    assert rig.control.events()[0]["phase"] == "rejected"
    assert not any(
        argv[0] in {"/usr/bin/systemctl", "/usr/bin/docker"} for argv in rig.calls
    )


def test_exact_stop_proof_quarantine_and_restart_retain_unknown_quota(rig):
    with sqlite3.connect(rig.controller) as database:
        before = (
            database.execute("SELECT * FROM quota_reservations").fetchall(),
            database.execute("SELECT * FROM quota_pools").fetchall(),
        )
    assert rig.control.tick() is False
    event = rig.control.events()[0]
    assert event["phase"] == "complete"
    proof = Path(event["payload"]["proof"])
    assert proof.stat().st_mode & 0o777 == 0o600
    assert proof.parent.stat().st_mode & 0o777 == 0o700
    stop = next(
        i for i, argv in enumerate(rig.calls) if argv[:2] == ("/usr/bin/docker", "stop")
    )
    cli = next(i for i, argv in enumerate(rig.calls) if argv[1] == "run")
    assert stop < cli
    assert all(info["State"]["Running"] for info in rig.state.values())
    with sqlite3.connect(rig.controller) as database:
        assert before == (
            database.execute("SELECT * FROM quota_reservations").fetchall(),
            database.execute("SELECT * FROM quota_pools").fetchall(),
        )
        assert database.execute(
            "SELECT state FROM resource_allocations"
        ).fetchone() == ("RELEASED",)
    count = len(rig.calls)
    assert rig.control.tick() is False
    assert len(rig.calls) == count


def test_lost_stop_cli_and_comment_responses_reconcile_without_second_intent(rig):
    rig.faults.update(lose_stop=True, lose_cli=True, lose_report=True)
    assert rig.control.tick() is True
    assert rig.control.events()[0]["phase"] == "worker_stopping"
    assert rig.control.tick() is False
    assert rig.control.events()[0]["phase"] == "complete"
    assert len([argv for argv in rig.calls if argv[1] == "run"]) == 1
    rig.control.tick()
    assert len(rig.control.events()) == 1 and len(rig.reports) == 1


def test_changed_worker_on_retry_is_never_stopped(rig):
    rig.faults["lose_stop"] = True
    assert rig.control.tick() is True
    replacement = "4" * 64
    rig.state[replacement] = {
        **rig.state[rig.services["coding-worker"]],
        "Id": replacement,
    }
    rig.services["coding-worker"] = replacement
    assert rig.control.tick() is True
    assert rig.control.events()[0]["error"] == "recovery_release_or_container_changed"
    assert not any(
        argv[:2] == ("/usr/bin/docker", "stop") and argv[-1] == replacement
        for argv in rig.calls
    )


def test_fresh_ops_event_reauthorizes_original_intent_after_bounded_retries(rig):
    original = rig.control.execute
    rig.control.execute = lambda _event: (_ for _ in ()).throw(
        AbandonBlocked("restart_pending")
    )
    for _ in range(3):
        assert rig.control.tick() is True
    rig.control.tick()
    assert rig.control.events()[0]["attempts"] == 3
    rig.comments.append(
        command(
            id=11,
            node_id="IC_retry",
            body="/agentd abandon run-1",
            issue_url="https://api.github.com/repos/benednied/agentd/issues/86",
        )
    )
    rig.control.execute = original
    assert rig.control.tick() is False
    events = rig.control.events()
    assert [row["phase"] for row in events] == ["complete", "retry_authorized"]
    proof = json.loads(Path(events[0]["payload"]["proof"]).read_text())
    assert proof["event_id"] == "github:1328873039:comment:10"
    assert proof["actor"] == "maintainer"
    assert events[1]["payload"]["origin_event_id"] == proof["event_id"]


def test_committed_quarantine_after_reboot_never_stops_a_new_worker_run(rig):
    original = rig.control.phase
    interrupted = [False]

    def crash_after_commit(event, phase):
        if phase == "quarantined" and not interrupted[0]:
            interrupted[0] = True
            raise AbandonBlocked("synthetic_host_restart")
        original(event, phase)

    rig.control.phase = crash_after_commit
    assert rig.control.tick() is True
    assert rig.control.events()[0]["phase"] == "diagnostics_started"
    for info in rig.state.values():
        info["State"] = {"Running": True, "Pid": 101}
    with sqlite3.connect(rig.controller) as database:
        other = {**rig.run, "id": "run-2", "job_id": "job-2"}
        database.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?)",
            ("run-2", "job-2", "RUNNING", "2026-10-03T13:00:00Z", json.dumps(other)),
        )
    rig.control.phase = original
    previous_stops = [
        argv for argv in rig.calls if argv[:2] == ("/usr/bin/docker", "stop")
    ]
    assert rig.control.tick() is False
    assert rig.control.events()[0]["phase"] == "complete"
    assert previous_stops == [
        argv for argv in rig.calls if argv[:2] == ("/usr/bin/docker", "stop")
    ]
    with sqlite3.connect(rig.controller) as database:
        assert database.execute(
            "SELECT state FROM runs WHERE id='run-2'"
        ).fetchone() == ("RUNNING",)


def test_lost_post_and_temporarily_absent_comment_never_posts_again(rig):
    rig.faults["lose_report"] = True
    assert rig.control.tick() is False
    assert len(rig.reports) == 1
    original = rig.control.watcher.status_api

    def temporarily_absent(endpoint, **kwargs):
        return [] if kwargs.get("paginated") else original(endpoint, **kwargs)

    rig.control.watcher.status_api = temporarily_absent
    assert rig.control.tick() is False
    assert len(rig.reports) == 1
    assert rig.control.events()[0]["reported_body"] is None
    rig.control.watcher.status_api = original
    assert rig.control.tick() is False
    assert len(rig.reports) == 1
    assert rig.control.events()[0]["reported_body"] == rig.reports[0]["body"]


def test_known_result_after_controller_fence_restores_without_worker_stop(rig):
    original = rig.control.units

    def completed_during_fence(action, *services):
        original(action, *services)
        if action == "stop" and services == ("coding-controller", "coding-publisher"):
            with sqlite3.connect(rig.journal) as database:
                database.execute(
                    "INSERT INTO worker_run_results VALUES(?,?,?,?)",
                    (
                        "node-1",
                        "epoch-1",
                        "run-1",
                        '{"usage":{"quota_units":17},"metadata":{"telemetry_valid":true}}',
                    ),
                )

    rig.control.units = completed_during_fence
    assert rig.control.tick() is False
    assert rig.control.events()[0]["phase"] == "declined"
    assert all(info["State"]["Running"] for info in rig.state.values())
    assert not any(
        argv[:2] == ("/usr/bin/docker", "stop") or argv[1] == "run"
        for argv in rig.calls
    )
