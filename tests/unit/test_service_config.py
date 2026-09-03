from dataclasses import replace
from pathlib import Path

import pytest

from agentd.config import ServiceConfig


def test_service_config_has_portable_xdg_defaults() -> None:
    config = ServiceConfig.from_environment({"HOME": "/home/operator"})

    assert config.database == Path("/home/operator/.local/state/agentd/state.sqlite")
    assert config.workspace_root == Path(
        "/home/operator/.local/share/agentd/workspaces"
    )
    assert config.codex_home == Path("/home/operator/.local/share/agentd/codex-home")
    assert config.uv_cache == Path("/home/operator/.cache/uv")
    assert config.uv_python_install_directory == Path("/home/operator/.cache/uv/python")
    assert config.model == "gpt-5.6-terra"
    assert config.reasoning_effort == "medium"
    assert config.worker_heartbeat_seconds == 15
    assert config.account_poll_seconds == 60
    assert config.account_stale_seconds == 300
    assert config.quota_top_up_tokens == 25_000
    assert config.hard_cap_grace_seconds == 120
    assert config.provider_stop_remaining_fraction == 0.02
    assert config.provider_reset_remaining is None


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
            "AGENTD_WORKER_HEARTBEAT_SECONDS": "12.5",
            "AGENTD_ACCOUNT_POLL_SECONDS": "30",
            "AGENTD_ACCOUNT_STALE_SECONDS": "120",
            "AGENTD_QUOTA_TOP_UP_TOKENS": "50000",
            "AGENTD_HARD_CAP_GRACE_SECONDS": "90",
            "AGENTD_PROVIDER_STOP_REMAINING_FRACTION": "0.02",
            "AGENTD_PROVIDER_RESET_REMAINING": "750000",
            "AGENTD_LOG_LEVEL": "DEBUG",
            "AGENTD_LOG_FORMAT": "text",
        }
    )

    assert config.database == Path("/state/agentd.sqlite")
    assert config.workspace_root == Path("/workspaces")
    assert config.codex_home == Path("/codex-home")
    assert config.uv_cache == Path("/uv-cache")
    assert config.uv_python_install_directory == Path("/uv-cache/python")
    assert config.reasoning_effort == "medium"
    assert config.poll_interval_seconds == 2.5
    assert config.worker_heartbeat_seconds == 12.5
    assert config.account_poll_seconds == 30
    assert config.account_stale_seconds == 120
    assert config.quota_top_up_tokens == 50_000
    assert config.hard_cap_grace_seconds == 90
    assert config.provider_stop_remaining_fraction == 0.02
    assert config.provider_reset_remaining == 750_000
    assert config.log_level == "DEBUG"
    assert not config.log_json


@pytest.mark.parametrize(
    "name",
    [
        "AGENTD_POLL_SECONDS",
        "AGENTD_WORKER_HEARTBEAT_SECONDS",
        "AGENTD_ACCOUNT_POLL_SECONDS",
        "AGENTD_ACCOUNT_STALE_SECONDS",
        "AGENTD_QUOTA_TOP_UP_TOKENS",
        "AGENTD_HARD_CAP_GRACE_SECONDS",
    ],
)
def test_service_config_rejects_nonpositive_controls(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        ServiceConfig.from_environment({name: "0"})


@pytest.mark.parametrize(
    "name",
    [
        "AGENTD_POLL_SECONDS",
        "AGENTD_WORKER_HEARTBEAT_SECONDS",
        "AGENTD_ACCOUNT_POLL_SECONDS",
        "AGENTD_ACCOUNT_STALE_SECONDS",
        "AGENTD_QUOTA_TOP_UP_TOKENS",
        "AGENTD_HARD_CAP_GRACE_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf"])
def test_service_config_rejects_nonfinite_environment_controls(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=name):
        ServiceConfig.from_environment({name: value})


@pytest.mark.parametrize(
    "name",
    [
        "poll_interval_seconds",
        "worker_heartbeat_seconds",
        "account_poll_seconds",
        "account_stale_seconds",
        "quota_top_up_tokens",
        "hard_cap_grace_seconds",
    ],
)
def test_service_config_rejects_nonfinite_programmatic_controls(name: str) -> None:
    config = ServiceConfig.from_environment({"HOME": "/home/operator"})

    with pytest.raises(ValueError, match=name):
        replace(config, **{name: float("nan")})


@pytest.mark.parametrize(
    "value",
    ["0", "-0.1", "0.01", "0.03", "1.1", "nan", "inf"],
)
def test_service_config_rejects_invalid_provider_stop_fraction(value: str) -> None:
    with pytest.raises(ValueError, match="AGENTD_PROVIDER_STOP"):
        ServiceConfig.from_environment(
            {"AGENTD_PROVIDER_STOP_REMAINING_FRACTION": value}
        )


def test_service_config_rejects_invalid_programmatic_provider_stop_fraction() -> None:
    config = ServiceConfig.from_environment({"HOME": "/home/operator"})

    with pytest.raises(ValueError, match=r"exactly 0\.02"):
        replace(config, provider_stop_remaining_fraction=0.03)


@pytest.mark.parametrize("value", ["-0.1", "nan", "inf"])
def test_service_config_rejects_invalid_provider_reset_remaining(value: str) -> None:
    with pytest.raises(ValueError, match="AGENTD_PROVIDER_RESET_REMAINING"):
        ServiceConfig.from_environment({"AGENTD_PROVIDER_RESET_REMAINING": value})


def test_service_config_represents_nonproduction_model_policy() -> None:
    config = ServiceConfig.from_environment(
        {
            "AGENTD_CODEX_MODEL": "gpt-5.6-sol",
            "AGENTD_CODEX_REASONING_EFFORT": "high",
        }
    )

    assert config.model == "gpt-5.6-sol"
    assert config.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"AGENTD_LOG_LEVEL": "verbose"}, "AGENTD_LOG_LEVEL"),
        ({"AGENTD_LOG_FORMAT": "xml"}, "AGENTD_LOG_FORMAT"),
    ],
)
def test_service_config_rejects_unknown_logging_controls(
    values: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ServiceConfig.from_environment(values)
