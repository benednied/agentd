from pathlib import Path

import pytest

from agentd.config import ServiceConfig


def test_service_config_has_hardened_hp_defaults() -> None:
    config = ServiceConfig.from_environment({})

    assert config.database == Path("/home/bened/.local/state/agentd/state.sqlite")
    assert config.workspace_root == Path("/home/bened/.local/share/agentd/workspaces")
    assert config.codex_home == Path("/home/bened/.local/share/agentd/codex-home")
    assert config.uv_cache == Path("/home/bened/.cache/uv")
    assert config.model == "gpt-5.6-terra"
    assert config.reasoning_effort == "medium"
    assert config.account_poll_seconds == 60
    assert config.account_stale_seconds == 300
    assert config.quota_top_up_tokens == 25_000
    assert config.hard_cap_grace_seconds == 120


def test_service_config_parses_explicit_environment() -> None:
    config = ServiceConfig.from_environment(
        {
            "AGENTD_DB": "/state/agentd.sqlite",
            "AGENTD_WORKSPACE_ROOT": "/workspaces",
            "AGENTD_CODEX_HOME": "/codex-home",
            "UV_CACHE_DIR": "/uv-cache",
            "AGENTD_CODEX_MODEL": "gpt-5.6-terra",
            "AGENTD_CODEX_REASONING_EFFORT": "medium",
            "AGENTD_POLL_SECONDS": "2.5",
            "AGENTD_ACCOUNT_POLL_SECONDS": "30",
            "AGENTD_ACCOUNT_STALE_SECONDS": "120",
            "AGENTD_QUOTA_TOP_UP_TOKENS": "50000",
            "AGENTD_HARD_CAP_GRACE_SECONDS": "90",
        }
    )

    assert config.database == Path("/state/agentd.sqlite")
    assert config.workspace_root == Path("/workspaces")
    assert config.codex_home == Path("/codex-home")
    assert config.uv_cache == Path("/uv-cache")
    assert config.reasoning_effort == "medium"
    assert config.poll_interval_seconds == 2.5
    assert config.account_poll_seconds == 30
    assert config.account_stale_seconds == 120
    assert config.quota_top_up_tokens == 50_000
    assert config.hard_cap_grace_seconds == 90


@pytest.mark.parametrize(
    "name",
    [
        "AGENTD_POLL_SECONDS",
        "AGENTD_ACCOUNT_POLL_SECONDS",
        "AGENTD_ACCOUNT_STALE_SECONDS",
        "AGENTD_QUOTA_TOP_UP_TOKENS",
        "AGENTD_HARD_CAP_GRACE_SECONDS",
    ],
)
def test_service_config_rejects_nonpositive_controls(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        ServiceConfig.from_environment({name: "0"})


def test_service_config_rejects_nonproduction_effort() -> None:
    with pytest.raises(ValueError, match="fixed to medium"):
        ServiceConfig.from_environment({"AGENTD_CODEX_REASONING_EFFORT": "high"})


def test_service_config_rejects_nonproduction_model() -> None:
    with pytest.raises(ValueError, match=r"fixed to gpt-5\.6-terra"):
        ServiceConfig.from_environment({"AGENTD_CODEX_MODEL": "gpt-5.6-sol"})
