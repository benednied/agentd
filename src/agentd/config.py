"""Validated service configuration shared by the daemon and deployment boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    value = default if raw is None else float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
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

    def __post_init__(self) -> None:
        if self.model != "gpt-5.6-terra":
            raise ValueError("Production Codex model is fixed to gpt-5.6-terra")
        if self.reasoning_effort != "medium":
            raise ValueError("Production Codex reasoning effort is fixed to medium")
        numeric = {
            "poll_interval_seconds": self.poll_interval_seconds,
            "account_poll_seconds": self.account_poll_seconds,
            "account_stale_seconds": self.account_stale_seconds,
            "quota_top_up_tokens": self.quota_top_up_tokens,
            "hard_cap_grace_seconds": self.hard_cap_grace_seconds,
        }
        for name, value in numeric.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> ServiceConfig:
        """Load configuration without consulting global process state directly."""

        return cls(
            database=Path(
                values.get(
                    "AGENTD_DB",
                    "/home/bened/.local/state/agentd/state.sqlite",
                )
            ).expanduser(),
            workspace_root=Path(
                values.get(
                    "AGENTD_WORKSPACE_ROOT",
                    "/home/bened/.local/share/agentd/workspaces",
                )
            ).expanduser(),
            codex_home=Path(
                values.get(
                    "AGENTD_CODEX_HOME",
                    "/home/bened/.local/share/agentd/codex-home",
                )
            ).expanduser(),
            uv_cache=Path(
                values.get("UV_CACHE_DIR", "/home/bened/.cache/uv")
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
        )
