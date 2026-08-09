from collections.abc import Callable

import pytest

from agentd.domain.models import EffortEstimate, Job, QuotaBudget


@pytest.fixture
def make_job() -> Callable[..., Job]:
    def factory(**overrides: object) -> Job:
        values: dict[str, object] = {
            "project": "example",
            "repository": "/tmp/example",
            "objective": "implement the requested change",
            "quota_budget": QuotaBudget(
                implementation=10,
                review=2,
                repair=3,
                validation=1,
            ),
            "effort": EffortEstimate(p50=10, p90=20, p99=40),
        }
        values.update(overrides)
        return Job(**values)  # type: ignore[arg-type]

    return factory
