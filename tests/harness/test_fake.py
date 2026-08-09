import asyncio
from collections.abc import Iterator

import pytest

from agentd.domain.enums import RunOutcome
from agentd.domain.models import ExecutionContract, RunHandle, RunResult
from agentd.harness import FakeHarnessCall, FakeHarnessDriver, UnknownRunError


def test_fake_driver_returns_configured_result_and_records_calls(
    execution_contract: ExecutionContract,
    deterministic_ids: Iterator[str],
) -> None:
    expected = RunResult(
        outcome=RunOutcome.COMPLETED,
        summary="scripted completion",
        commit="abc123",
        consumed_quota=4,
    )
    driver = FakeHarnessDriver(
        result=expected,
        id_factory=lambda: next(deterministic_ids),
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.steer(run, "Run the focused tests")
        await driver.interrupt(run)
        result = await driver.collect(run)

        assert run == RunHandle(
            id="run-1",
            driver="fake",
            external_id="fake:run-1",
        )
        assert result is expected
        assert driver.execution_for(run) is execution_contract
        assert driver.calls_for(run) == (
            FakeHarnessCall("start"),
            FakeHarnessCall("steer", "Run the focused tests"),
            FakeHarnessCall("interrupt"),
            FakeHarnessCall("collect"),
        )

    asyncio.run(scenario())


def test_fake_driver_configures_results_per_execution(
    execution_contract: ExecutionContract,
) -> None:
    driver = FakeHarnessDriver(
        result_factory=lambda execution: RunResult(
            outcome=RunOutcome.COMPLETED,
            summary=execution.job_id,
        ),
        id_factory=lambda: "run-1",
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        result = await driver.collect(run)
        assert result.summary == "job-1"

    asyncio.run(scenario())


def test_fake_driver_cancel_has_cancelled_outcome(
    execution_contract: ExecutionContract,
) -> None:
    driver = FakeHarnessDriver(id_factory=lambda: "run-1")

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.cancel(run)

        assert (await driver.collect(run)).outcome is RunOutcome.CANCELLED
        assert tuple(call.operation for call in driver.calls_for(run)) == (
            "start",
            "cancel",
            "collect",
        )

    asyncio.run(scenario())


def test_fake_driver_rejects_foreign_run(
    execution_contract: ExecutionContract,
) -> None:
    driver = FakeHarnessDriver()

    async def scenario() -> None:
        run = RunHandle(id="missing", driver="other")
        with pytest.raises(UnknownRunError, match="other"):
            await driver.collect(run)

    asyncio.run(scenario())


def test_fake_driver_rejects_ambiguous_result_configuration() -> None:
    result = RunResult(outcome=RunOutcome.COMPLETED)
    with pytest.raises(ValueError, match="either result or result_factory"):
        FakeHarnessDriver(result=result, result_factory=lambda _execution: result)
