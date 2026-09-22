import hashlib
import subprocess
import sys
from dataclasses import replace

import pytest

from agentd.publication import (
    CollectedCodingResult,
    DraftPublisher,
    PublicationError,
    PublicationIntent,
    PublicationPending,
    PublicationStore,
    TrustedFinalizer,
)


class TestProcessRunner:
    # Tests use their own trusted tiny commands. Production has no local bypass.
    def run(self, command, *, cwd, env, timeout):
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
        )


def finalizer():
    return TrustedFinalizer(TestProcessRunner())


def git(path, *args):
    return subprocess.run(
        ("git", "-C", str(path), *args), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def result(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "file").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("result")
    git(repo, "commit", "-am", "result")
    commit = git(repo, "rev-parse", "HEAD")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('file').read_text() == 'result'",
    )
    intent = PublicationIntent(
        "job",
        "owner/repo",
        12,
        "revision",
        "master",
        base,
        commit,
        "worker",
        "run",
        "profile-1",
        (command,),
    )
    collected = CollectedCodingResult(
        "job", "owner/repo", base, commit, "worker", "run", True
    )
    return repo, intent, collected


class Adapter:
    def __init__(self, fail=None):
        self.head = None
        self.pr = None
        self.pushes = self.creates = 0
        self.fail = fail

    def branch_commit(self, intent):
        return self.head

    def push(self, intent, repository):
        self.pushes += 1
        self.head = intent.result_commit
        if self.fail == "push":
            self.fail = None
            raise ConnectionError("lost push acknowledgement")

    def find_pr(self, intent):
        return self.pr

    def create_draft(self, intent, body):
        self.creates += 1
        self.pr = {
            "headRefName": intent.branch,
            "headRefOid": intent.result_commit,
            "baseRefName": intent.base_branch,
            "isDraft": True,
            "isCrossRepository": False,
            "body": body,
            "url": "https://github.com/owner/repo/pull/1",
        }
        if self.fail == "create":
            self.fail = None
            raise ConnectionError("lost create acknowledgement")
        return self.pr


@pytest.mark.parametrize("failure", [None, "push", "create"])
def test_reconcile_after_restart_without_coding_or_revalidation(
    result, tmp_path, failure
):
    repo, intent, collected = result
    adapter = Adapter(failure)
    database = tmp_path / "state.db"
    publisher = DraftPublisher(PublicationStore(database), adapter, finalizer())
    if failure:
        with pytest.raises(ConnectionError):
            publisher.publish(intent, collected, repo)
    else:
        publisher.publish(intent, collected, repo)
    # Restart can publish without access to the worker or validation checkout.
    publisher = DraftPublisher(PublicationStore(database), adapter, finalizer())
    pr = publisher.publish(intent, collected, tmp_path / "unavailable")
    assert publisher.publish(intent, collected, repo) == pr
    assert adapter.pushes == adapter.creates == 1
    state = publisher.store.bind(intent)
    assert state["stage"] == "published"
    assert state["evidence"][0]["commit"] == intent.result_commit
    assert state["evidence"][0]["returncode"] == 0
    for value in (
        intent.source_revision,
        intent.base_commit,
        intent.result_commit,
        intent.worker_id,
        intent.run_id,
        intent.job_id,
    ):
        assert value in pr["body"]


def test_ambiguous_create_never_blindly_replayed(result, tmp_path):
    repo, intent, collected = result
    adapter = Adapter("create")
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(ConnectionError):
        publisher.publish(intent, collected, repo)
    adapter.pr = None  # Eventual consistency or a lost request: cannot tell.
    with pytest.raises(PublicationPending, match="unresolved"):
        publisher.publish(intent, collected, repo)
    assert adapter.creates == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_id", "other"),
        ("run_id", "other"),
        ("repository", "evil/repo"),
        ("succeeded", False),
    ],
)
def test_collected_identity_is_not_model_assertion(result, tmp_path, field, value):
    repo, intent, collected = result
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), Adapter(), finalizer()
    )
    with pytest.raises(PublicationError, match="controller intent"):
        publisher.publish(intent, replace(collected, **{field: value}), repo)


def test_job_result_cannot_be_rebound(result, tmp_path):
    _, intent, _ = result
    store = PublicationStore(tmp_path / "state.db")
    store.bind(intent)
    with pytest.raises(PublicationError, match="different publication"):
        store.bind(replace(intent, result_commit="a" * 40))


@pytest.mark.parametrize(
    "code",
    [
        "raise SystemExit(7)",
        "import time; time.sleep(10)",
        "from pathlib import Path; Path('file').write_text('tamper')",
    ],
)
def test_validation_failure_persisted_and_never_published(result, tmp_path, code):
    repo, intent, collected = result
    intent = replace(
        intent,
        validation_commands=((sys.executable, "-c", code),),
        validation_timeout_seconds=0.1,
    )
    adapter = Adapter()
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(PublicationError, match="validation failed"):
        publisher.publish(intent, collected, repo)
    with pytest.raises(PublicationError, match="validation failed"):
        publisher.publish(intent, collected, repo)
    assert adapter.pushes == adapter.creates == 0
    assert publisher.store.bind(intent)["stage"] == "validation_failed"


def test_credentials_not_in_validation_environment(result, monkeypatch):
    repo, intent, collected = result
    monkeypatch.setenv("GH_TOKEN", "secret")
    code = (
        "import os; assert 'GH_TOKEN' not in os.environ; "
        "assert 'SSH_AUTH_SOCK' not in os.environ"
    )
    intent = replace(intent, validation_commands=((sys.executable, "-c", code),))
    assert finalizer().validate(intent, collected, repo)[0]["returncode"] == 0


def test_conflicting_remote_branch_blocks_publication(result, tmp_path):
    repo, intent, collected = result
    adapter = Adapter()
    adapter.head = "a" * 40
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(PublicationError, match="conflicting result"):
        publisher.publish(intent, collected, repo)
    assert adapter.pushes == adapter.creates == 0


def test_unrelated_result_is_rejected(result):
    repo, intent, collected = result
    git(repo, "checkout", "--orphan", "unrelated")
    git(repo, "commit", "-m", "unrelated")
    commit = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="Git operation"):
        finalizer().validate(
            replace(intent, result_commit=commit),
            replace(collected, result_commit=commit),
            repo,
        )


def test_branch_identity_uses_full_job_digest(result):
    _, intent, _ = result
    assert intent.branch.endswith(hashlib.sha256(b"owner/repo\0job").hexdigest())
    assert replace(intent, job_id="job!").branch != intent.branch


@pytest.mark.parametrize(
    "change",
    [
        {"isDraft": False},
        {"isCrossRepository": True},
        {"headRefOid": "a" * 40},
        {"baseRefName": "other"},
        {"body": "unowned"},
    ],
)
def test_existing_pr_conflicts_fail_closed(result, tmp_path, change):
    repo, intent, collected = result
    adapter = Adapter()
    adapter.head = intent.result_commit
    adapter.create_draft(intent, DraftPublisher._body(intent))
    adapter.pr.update(change)
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(PublicationError, match="intended draft"):
        publisher.publish(intent, collected, repo)
    assert adapter.creates == 1


def bundle_evidence(repo, intent, tmp_path):
    import base64

    path = tmp_path / "result.bundle"
    git(repo, "bundle", "create", str(path), "HEAD", f"^{intent.base_commit}")
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode()
    return {
        "job_id": intent.job_id,
        "run_id": intent.run_id,
        "repository": intent.repository,
        "base_commit": intent.base_commit,
        "result_commit": intent.result_commit,
        "source_revision": intent.source_revision,
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_chunks": [
            encoded[i : i + 16000] for i in range(0, len(encoded), 16000)
        ],
    }


def test_bundle_import_verified_then_validated_from_custom_retention_ref(
    result, tmp_path
):
    from agentd.publication import import_coding_bundle

    repo, intent, collected = result
    evidence = bundle_evidence(repo, intent, tmp_path)
    # Controller cache has only original base reachable through its branch.
    cache = tmp_path / "cache"
    git(tmp_path, "clone", "--bare", str(repo), str(cache))
    git(cache, "update-ref", "refs/heads/master", intent.base_commit)
    git(cache, "reflog", "expire", "--expire=now", "--all")
    git(cache, "gc", "--prune=now")
    ref = import_coding_bundle(intent, collected, evidence, cache)
    assert git(cache, "rev-parse", ref) == intent.result_commit
    assert import_coding_bundle(intent, collected, evidence, cache) == ref
    assert finalizer().validate(intent, collected, cache)[0]["returncode"] == 0


def test_bundle_import_fetches_new_authorized_base_before_verification(
    result, tmp_path, monkeypatch
):
    """An old cache must fetch the controller-approved base before bundle verify."""
    from agentd import publication
    from agentd.publication import ensure_authorized_base, import_coding_bundle

    repo, original, _ = result
    cache = tmp_path / "cache"
    git(tmp_path, "clone", "--bare", str(repo), str(cache))
    git(cache, "update-ref", "refs/heads/master", original.base_commit)
    git(cache, "reflog", "expire", "--expire=now", "--all")
    git(cache, "gc", "--prune=now")
    (repo / "file").write_text("authorized base")
    git(repo, "commit", "-am", "authorized base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("worker result")
    git(repo, "commit", "-am", "worker result")
    result_commit = git(repo, "rev-parse", "HEAD")
    intent = replace(original, base_commit=base, result_commit=result_commit)
    collected = CollectedCodingResult(
        "job", "owner/repo", base, result_commit, "worker", "run", True
    )
    evidence = bundle_evidence(repo, intent, tmp_path)
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "push", str(remote), "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        git(cache, "cat-file", "-e", f"{base}^{{commit}}")
    real_git = publication._git

    def map_profile_remote(repository, *args, **kwargs):
        args = tuple(
            str(remote) if arg == "https://github.com/owner/repo.git" else arg
            for arg in args
        )
        return real_git(repository, *args, **kwargs)

    monkeypatch.setattr(publication, "_git", map_profile_remote)
    ensure_authorized_base(
        cache, "owner/repo", "https://github.com/owner/repo.git", base
    )
    original_mapped = publication._git

    def deny_fetch(repository, *args, **kwargs):
        if "fetch" in args:
            raise AssertionError("retry unexpectedly fetched an already retained base")
        return original_mapped(repository, *args, **kwargs)

    monkeypatch.setattr(publication, "_git", deny_fetch)
    ensure_authorized_base(
        cache, "owner/repo", "https://github.com/owner/repo.git", base
    )
    monkeypatch.setattr(publication, "_git", original_mapped)
    ref = import_coding_bundle(intent, collected, evidence, cache)
    assert git(cache, "rev-parse", ref) == result_commit
    with pytest.raises(PublicationError):
        ensure_authorized_base(
            cache, "owner/repo", "https://evil.example/repo.git", base
        )


@pytest.mark.parametrize(
    "change",
    [
        {"bundle_sha256": "bad"},
        {"bundle_chunks": ["!"]},
        {"run_id": "unowned"},
        {"source_revision": "edited"},
    ],
)
def test_bad_bundle_is_rejected_before_git(result, tmp_path, change):
    from agentd.publication import import_coding_bundle

    repo, intent, collected = result
    evidence = bundle_evidence(repo, intent, tmp_path)
    evidence.update(change)
    with pytest.raises(PublicationError):
        import_coding_bundle(intent, collected, evidence, tmp_path / "does-not-exist")


def test_missing_branch_does_not_reset_ambiguous_pr_creation(result, tmp_path):
    repo, intent, collected = result
    adapter = Adapter("create")
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(ConnectionError):
        publisher.publish(intent, collected, repo)
    adapter.head = None
    with pytest.raises(PublicationPending):
        publisher.publish(intent, collected, repo)
    assert publisher.store.bind(intent)["stage"] == "create_requested"
    assert adapter.creates == adapter.pushes == 1


def test_real_push_is_create_only_and_cannot_overwrite_conflicting_branch(
    result, tmp_path, monkeypatch
):
    import agentd.publication as publication

    repo, intent, _ = result
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    original_git = publication._git

    def local_git(repository, *args, **kwargs):
        args = tuple(
            str(remote) if arg == "https://github.com/owner/repo.git" else arg
            for arg in args
        )
        return original_git(repository, *args, **kwargs)

    monkeypatch.setattr(publication, "_git", local_git)
    adapter = publication.GitHubPublicationAdapter()
    adapter.push(intent, repo)
    assert (
        git(remote, "rev-parse", f"refs/heads/{intent.branch}") == intent.result_commit
    )
    git(remote, "update-ref", f"refs/heads/{intent.branch}", intent.base_commit)
    with pytest.raises(PublicationError, match="Git operation"):
        adapter.push(intent, repo)
    assert git(remote, "rev-parse", f"refs/heads/{intent.branch}") == intent.base_commit


def test_remote_lookup_errors_are_not_absence(result, monkeypatch):
    from agentd.publication import GitHubPublicationAdapter

    _, intent, _ = result
    adapter = GitHubPublicationAdapter()
    monkeypatch.setattr(
        adapter, "_gh", lambda *args: {"errors": [{"message": "unavailable"}]}
    )
    with pytest.raises(PublicationPending):
        adapter.branch_commit(intent)


def test_default_finalizer_fails_closed_without_containment(result, tmp_path):
    repo, intent, collected = result
    adapter = Adapter()
    publisher = DraftPublisher(PublicationStore(tmp_path / "state.db"), adapter)
    with pytest.raises(PublicationError, match="contained validation runner"):
        publisher.publish(intent, collected, repo)
    assert adapter.pushes == adapter.creates == 0


def test_bubblewrap_constructs_private_credential_free_boundary(tmp_path, monkeypatch):
    import agentd.publication as publication

    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(publication, "_run", capture)
    runner = publication.BubblewrapValidationRunner()
    runner.run(
        ("python", "-m", "pytest"),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "GH_TOKEN": "never-pass-this"},
        timeout=60,
    )
    command, kwargs = calls[0]
    assert all(
        flag in command
        for flag in (
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
        )
    )
    assert "--proc" not in command
    assert "--die-with-parent" in command
    assert "--clearenv" in command
    assert "never-pass-this" not in command
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
    assert command[command.index("--bind") + 1 : command.index("--bind") + 3] == [
        str(tmp_path),
        "/workspace",
    ]
    assert command.count("--bind") == 1
    assert not any(path in command for path in ("/home", "/root", "/Users", "/etc"))


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_really_denies_secrets_and_network(tmp_path):
    from agentd.publication import MacOSSandboxValidationRunner

    secret = tmp_path / "secret"
    secret.write_text("credential")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "escape").symlink_to(secret)
    command = f"""import os,pathlib,socket
assert 'GH_TOKEN' not in os.environ
for path in ({str(secret)!r}, 'escape'):
    try:
        pathlib.Path(path).read_text()
    except PermissionError:
        pass
    else:
        raise AssertionError('secret accessible')
s = socket.socket()
s.settimeout(1)
try:
    assert s.connect_ex(('192.0.2.1',443)) != 0
except PermissionError:
    pass
pathlib.Path('verified').write_text('contained')
"""
    result = MacOSSandboxValidationRunner().run(
        ("/usr/bin/python3", "-c", command),
        cwd=checkout,
        env={"PATH": "/usr/bin:/bin", "GH_TOKEN": "secret"},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (checkout / "verified").read_text() == "contained"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_exact_commit_validates_in_macos_sandbox(result):
    from agentd.publication import MacOSSandboxValidationRunner

    repo, intent, collected = result
    intent = replace(
        intent,
        validation_commands=(
            (
                "/usr/bin/python3",
                "-c",
                "from pathlib import Path; assert Path('file').read_text() == 'result'",
            ),
            ("/usr/bin/git", "diff", "--check"),
        ),
    )
    evidence = TrustedFinalizer(MacOSSandboxValidationRunner()).validate(
        intent, collected, repo
    )
    assert len(evidence) == 2
    assert all(item["returncode"] == 0 for item in evidence)


def test_observed_revocation_during_validation_blocks_all_publication(result, tmp_path):
    repo, intent, collected = result
    authorized = True

    class RevokingFinalizer:
        def validate(self, *args):
            nonlocal authorized
            evidence = finalizer().validate(*args)
            authorized = False
            return evidence

    def authorization_check():
        if not authorized:
            raise PublicationError("source authorization revoked")

    adapter = Adapter()
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, RevokingFinalizer()
    )
    with pytest.raises(PublicationError, match="revoked"):
        publisher.publish(
            intent, collected, repo, authorization_check=authorization_check
        )
    assert adapter.pushes == adapter.creates == 0
    assert publisher.store.bind(intent)["stage"] == "validated"


def test_observed_revocation_after_push_blocks_pr_creation(result, tmp_path):
    repo, intent, collected = result
    authorized = True

    class RevokingAdapter(Adapter):
        def push(self, *args):
            nonlocal authorized
            super().push(*args)
            authorized = False

    def authorization_check():
        if not authorized:
            raise PublicationError("source authorization revoked")

    adapter = RevokingAdapter()
    publisher = DraftPublisher(
        PublicationStore(tmp_path / "state.db"), adapter, finalizer()
    )
    with pytest.raises(PublicationError, match="revoked"):
        publisher.publish(
            intent, collected, repo, authorization_check=authorization_check
        )
    assert adapter.pushes == 1
    assert adapter.creates == 0
    assert publisher.store.bind(intent)["stage"] == "push_requested"


def test_trusted_postcheck_never_executes_validation_git_config(result, tmp_path):
    repo, intent, collected = result
    marker = tmp_path / "escaped"
    code = f"""from pathlib import Path
import subprocess
hook = Path('malicious-fsmonitor')
hook.write_text('#!/bin/sh\\ntouch {marker}\\n')
hook.chmod(0o755)
subprocess.run(['git', 'config', 'core.fsmonitor', str(hook.resolve())], check=True)
"""
    intent = replace(intent, validation_commands=((sys.executable, "-c", code),))
    evidence = finalizer().validate(intent, collected, repo)
    assert all(item["returncode"] == 0 for item in evidence)
    assert not marker.exists(), "trusted postcheck executed validation-owned hook"


def test_validation_cannot_hide_changes_using_tampered_git_index(result):
    repo, intent, collected = result
    code = """from pathlib import Path
import subprocess
subprocess.run(['git', 'update-index', '--assume-unchanged', 'file'], check=True)
Path('file').write_text('tampered')
"""
    intent = replace(intent, validation_commands=((sys.executable, "-c", code),))
    evidence = finalizer().validate(intent, collected, repo)
    assert evidence[-1]["error"] == "validation-mutated-result"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_runner_denies_git_metadata_writes(result):
    from agentd.publication import MacOSSandboxValidationRunner

    repo, _, _ = result
    code = """from pathlib import Path
try:
    Path('.git/config').write_text('malicious')
except PermissionError:
    pass
else:
    raise AssertionError('Git metadata writable')
"""
    result = MacOSSandboxValidationRunner().run(
        ("/usr/bin/python3", "-c", code),
        cwd=repo,
        env={"PATH": "/usr/bin:/bin"},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
