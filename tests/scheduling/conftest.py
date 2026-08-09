from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from agentd.domain.models import (
    EffortEstimate,
    Job,
    QuotaBudget,
    ResourceVector,
    WorkerNode,
)

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _make_job(**overrides: Any) -> Job:
    values: dict[str, Any] = {
        "id": "job",
        "project": "project",
        "repository": "/repo",
        "objective": "Implement a bounded change",
        "quota_budget": QuotaBudget(implementation=10, maximum=20),
        "effort": EffortEstimate(p50=5, p90=10, p99=20),
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    values.update(overrides)
    return Job(**values)


def _make_node(**overrides: Any) -> WorkerNode:
    values: dict[str, Any] = {
        "id": "node",
        "labels": {"os": "linux", "arch": "x86_64"},
        "capacity": ResourceVector(cpu=4, ram_gb=8),
        "harnesses": frozenset({"fake"}),
        "updated_at": FIXED_TIME,
    }
    values.update(overrides)
    return WorkerNode(**values)


@pytest.fixture(name="job_factory")
def fixture_job_factory() -> Callable[..., Job]:
    return _make_job


@pytest.fixture(name="node_factory")
def fixture_node_factory() -> Callable[..., WorkerNode]:
    return _make_node
