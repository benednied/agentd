"""Validated service configuration shared by the daemon and deployment boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    value = default if raw is None else float(raw)
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Host-local paths and conservative Codex policy controls.

    These values configure the control plane. They are never added to an
    :class:`~agentd.domain.models.ExecutionContract`, so quota scarcity and
    scheduler policy remain hidden from the worker model.
    """

    database: Path
    workspace_root: Path
    codex_home: Path
    uv_cache: Path
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    poll_interval_seconds: float = 1.0
    account_poll_seconds: float = 60.0
    account_stale_seconds: float = 300.0
    quota_top_up_tokens: float = 25_000.0
    hard_cap_grace_seconds: float = 120.0
    log_level: str = "INFO"
    log_json: bool = True

    def __post_init__(self) -> None:
        numeric = {
            "poll_interval_seconds": self.poll_interval_seconds,
            "account_poll_seconds": self.account_poll_seconds,
            "account_stale_seconds": self.account_stale_seconds,
            "quota_top_up_tokens": self.quota_top_up_tokens,
            "hard_cap_grace_seconds": self.hard_cap_grace_seconds,
        }
        for name, value in numeric.items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.log_level.upper() not in {
            "TRACE",
            "DEBUG",
            "INFO",
            "SUCCESS",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError("AGENTD_LOG_LEVEL is not a supported Loguru level")

    @property
    def uv_python_install_directory(self) -> Path:
        """Exact managed-Python toolchain path inside the dedicated uv cache."""

        return self.uv_cache / "python"

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> ServiceConfig:
        """Load configuration without consulting global process state directly."""

        log_format = values.get("AGENTD_LOG_FORMAT", "json").lower()
        if log_format not in {"json", "text"}:
            raise ValueError("AGENTD_LOG_FORMAT must be 'json' or 'text'")
        home = Path(values.get("HOME", str(Path.home()))).expanduser()
        state_home = Path(
            values.get("XDG_STATE_HOME", str(home / ".local" / "state"))
        ).expanduser()
        data_home = Path(
            values.get("XDG_DATA_HOME", str(home / ".local" / "share"))
        ).expanduser()
        cache_home = Path(
            values.get("XDG_CACHE_HOME", str(home / ".cache"))
        ).expanduser()
        return cls(
            database=Path(
                values.get(
                    "AGENTD_DB",
                    str(state_home / "agentd" / "state.sqlite"),
                )
            ).expanduser(),
            workspace_root=Path(
                values.get(
                    "AGENTD_WORKSPACE_ROOT",
                    str(data_home / "agentd" / "workspaces"),
                )
            ).expanduser(),
            codex_home=Path(
                values.get(
                    "AGENTD_CODEX_HOME",
                    str(data_home / "agentd" / "codex-home"),
                )
            ).expanduser(),
            uv_cache=Path(
                values.get("UV_CACHE_DIR", str(cache_home / "uv"))
            ).expanduser(),
            model=values.get("AGENTD_CODEX_MODEL", "gpt-5.6-terra"),
            reasoning_effort=values.get(
                "AGENTD_CODEX_REASONING_EFFORT",
                "medium",
            ),
            poll_interval_seconds=_positive_float(
                values,
                "AGENTD_POLL_SECONDS",
                1.0,
            ),
            account_poll_seconds=_positive_float(
                values,
                "AGENTD_ACCOUNT_POLL_SECONDS",
                60.0,
            ),
            account_stale_seconds=_positive_float(
                values,
                "AGENTD_ACCOUNT_STALE_SECONDS",
                300.0,
            ),
            quota_top_up_tokens=_positive_float(
                values,
                "AGENTD_QUOTA_TOP_UP_TOKENS",
                25_000.0,
            ),
            hard_cap_grace_seconds=_positive_float(
                values,
                "AGENTD_HARD_CAP_GRACE_SECONDS",
                120.0,
            ),
            log_level=values.get("AGENTD_LOG_LEVEL", "INFO"),
            log_json=log_format == "json",
        )
