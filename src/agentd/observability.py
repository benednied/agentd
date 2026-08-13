"""Central, secret-conscious Loguru configuration for agentd processes."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from loguru import logger

logger.disable("agentd")

_TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DDTHH:mm:ss.SSSZ}</green> | "
    "<level>{level: <8}</level> | {message} | {extra}"
)
_CONTEXT_FIELDS = frozenset(
    {
        "component",
        "operation",
        "job_id",
        "run_id",
        "workspace_id",
        "reservation_id",
        "pool_id",
        "node_id",
        "driver",
        "harness",
        "unit",
        "outcome",
        "error_type",
        "repair_turn",
        "cleanup_error_count",
        "cancelled",
    }
)


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
) -> None:
    """Install the single process-wide, exception-safe agentd log sink."""

    logger.remove()
    logger.enable("agentd")
    logger.add(
        sys.stderr,
        level=level.upper(),
        serialize=json_output,
        format=_TEXT_FORMAT,
        backtrace=False,
        diagnose=False,
        enqueue=True,
        catch=True,
    )


def event_logger(**context: str | int | float | bool | None) -> Any:
    """Bind allow-listed scalar identifiers to one operational event stream."""

    clean: Mapping[str, str | int | float | bool | None] = {
        key: value
        for key, value in context.items()
        if key in _CONTEXT_FIELDS and value is not None
    }
    return logger.bind(**clean)
