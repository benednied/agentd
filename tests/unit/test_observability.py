"""Operational logging configuration and data-minimization tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from io import StringIO

import pytest
from loguru import logger

from agentd.observability import configure_logging, event_logger


@pytest.fixture(autouse=True)
def reset_agentd_logging() -> Iterator[None]:
    """Keep process-global Loguru sinks isolated between tests."""

    yield
    logger.remove()
    logger.disable("agentd")


def test_json_logging_binds_operational_context() -> None:
    output = StringIO()
    configure_logging()
    logger.remove()
    logger.add(output, serialize=True, enqueue=False)

    event_logger(component="daemon", job_id="job-1", run_id="run-1").info(
        "dispatch_succeeded"
    )

    record = json.loads(output.getvalue())["record"]
    assert record["message"] == "dispatch_succeeded"
    assert record["extra"] == {
        "component": "daemon",
        "job_id": "job-1",
        "run_id": "run-1",
    }


def test_unknown_context_fields_cannot_leak_sensitive_values() -> None:
    output = StringIO()
    configure_logging()
    logger.remove()
    logger.add(output, serialize=True, enqueue=False)

    event_logger(
        component="harness",
        prompt="confidential worker content",
        access_token="secret-token",
    ).info("turn_started")

    rendered = output.getvalue()
    assert "confidential worker content" not in rendered
    assert "secret-token" not in rendered
