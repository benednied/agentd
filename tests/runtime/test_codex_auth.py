import base64
import io
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from openai_codex.errors import InternalRpcError
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from agentd.cli import main
from agentd.domain.models import ProviderQuotaSnapshot, QuotaPool
from agentd.harness import app_server, codex_auth
from agentd.harness.codex_auth import CodexSession
from agentd.state.sqlite import SQLiteStateStore


def _access_token(label, *, expires_in=3600):
    payload = json.dumps({"exp": time.time() + expires_in, "sub": label}).encode()
    return (
        "e30." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".synthetic"
    )


def _write_session(home, access, refresh):
    auth = home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "synthetic-account",
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": "synthetic-id-token",
                },
            }
        )
    )
    auth.chmod(0o600)
    return auth


def test_concurrent_rejected_access_shares_one_atomic_exchange(tmp_path, monkeypatch):
    old = _access_token("old")
    new = _access_token("new")
    auth = _write_session(tmp_path, old, "refresh-before")
    original = auth.read_bytes()
    barrier = Barrier(2, timeout=5)
    exchanges = []
    replace = codex_auth.os.replace

    def exchange(token):
        exchanges.append(token)
        return {"access_token": new, "refresh_token": "refresh-after"}

    def check_replace(source, target):
        assert target == auth
        assert auth.read_bytes() == original
        assert Path(source).stat().st_mode & 0o777 == 0o600
        assert json.loads(Path(source).read_bytes())["tokens"]["access_token"] == new
        replace(source, target)

    def caller():
        session = CodexSession(tmp_path, exchange=exchange)
        rejected = session.access()
        assert rejected.token == old
        barrier.wait()
        return session.access(rejected=rejected)

    monkeypatch.setattr(codex_auth.os, "replace", check_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert [
            result.token for result in executor.map(lambda _: caller(), range(2))
        ] == [new, new]
    assert exchanges == ["refresh-before"]
    assert json.loads(auth.read_bytes())["tokens"] == {
        "account_id": "synthetic-account",
        "access_token": new,
        "refresh_token": "refresh-after",
        "id_token": "synthetic-id-token",
    }


def test_transient_refresh_failure_is_shared_without_reauthorizing(tmp_path):
    access = _access_token("expired", expires_in=-1)
    _write_session(tmp_path, access, "refresh-before")
    exchanges = []

    def exchange(token):
        exchanges.append(token)
        raise codex_auth.CodexRefreshUnavailable("synthetic timeout")

    for _ in range(2):
        with pytest.raises(codex_auth.CodexRefreshUnavailable):
            CodexSession(tmp_path, exchange=exchange).access()
    assert exchanges == ["refresh-before"]


def test_quota_status_redacts_terminal_refresh_and_recovers_after_sign_in(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "service-codex"
    home.mkdir(mode=0o700)
    old = _access_token("rejected")
    new = _access_token("authorized")
    _write_session(home, old, "synthetic-rotated-secret")
    exchanges = []

    class RejectingOAuth:
        def open(self, request, *, timeout):
            assert request.full_url == "https://auth.openai.com/oauth/token"
            exchanges.append(json.loads(request.data)["refresh_token"])
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(
                    b'{"error":{"code":"refresh_token_reused",'
                    b'"message":"synthetic-upstream-secret"}}'
                ),
            )

    class ScriptedSDK:
        def __init__(self, config):
            assert config.env["CODEX_HOME"] == str(home)
            assert config.config_overrides == (app_server.EXTERNAL_AUTH_CONFIG,)
            self._sync = SimpleNamespace()

        async def start(self):
            pass

        async def initialize(self):
            return SimpleNamespace(
                serverInfo=None, userAgent=None, platformFamily=None, platformOs=None
            )

        async def account_login_start(self, params):
            assert params == {
                "type": "chatgptAuthTokens",
                "accessToken": params["accessToken"],
                "chatgptAccountId": "synthetic-account",
            }
            self.token = params["accessToken"]

        async def request(self, method, params, *, response_model):
            assert method == "account/rateLimits/read"
            assert response_model is GetAccountRateLimitsResponse
            if self.token == old:
                raise InternalRpcError(
                    -32603,
                    "failed to fetch codex rate limits: GET https://example.invalid "
                    "failed: 401 Unauthorized; body=synthetic-upstream-secret",
                )
            assert self.token == new
            return response_model.model_validate(
                {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 12}}}
            )

        async def close(self):
            pass

    def sign_in(command, *, cwd, env, check):
        assert command[-2:] == ["login", "--device-auth"]
        staging = Path(env["CODEX_HOME"])
        assert staging.parent == home and str(staging) == cwd
        _write_session(staging, new, "new-refresh-token")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_auth, "build_opener", lambda *_: RejectingOAuth())
    monkeypatch.setattr(app_server, "AsyncCodexClient", ScriptedSDK)
    monkeypatch.setattr("agentd.cli.subprocess.run", sign_in)
    monkeypatch.setattr("agentd.cli.configure_logging", lambda **_: None)
    database = tmp_path / "state.sqlite"
    args = ["--db", str(database), "--codex-home", str(home)]
    previous = ProviderQuotaSnapshot("codex", "codex", primary_used_percent=9)
    with SQLiteStateStore(database) as store:
        store.register_quota_pool(QuotaPool("codex", "openai", 200))
        store.append_provider_quota_snapshot(previous)
    for _ in range(2):
        assert main([*args, "codex-status"]) == 2
        captured = capsys.readouterr()
        assert "synthetic-" not in captured.out + captured.err
        report = json.loads(captured.out)
        assert report["auth_status"] == "reauthorization_required"
        assert report["reason"] == "refresh_token_reused"
        assert "codex-reauthorize" in report["action"]
    assert exchanges == ["synthetic-rotated-secret"]
    with SQLiteStateStore(database) as store:
        assert store.latest_provider_quota_snapshot("codex") == previous

    assert main([*args, "codex-reauthorize", "--device-auth"]) == 0
    capsys.readouterr()
    assert main([*args, "codex-status"]) == 0
    assert json.loads(capsys.readouterr().out)["primary_used_percent"] == 12
