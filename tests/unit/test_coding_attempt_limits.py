from dataclasses import replace

import pytest

from agentd.coding.attempts import CodingAttemptLimits, is_proven_preparation_failure
from agentd.coding.models import CodingWorkOrder
from agentd.domain.enums import RunOutcome, RunState
from agentd.domain.models import (
    CodingOperation,
    ExecutionContract,
    RunHandle,
    RunRecord,
    RunResult,
    TokenUsage,
)


def preparation_run():
    operation = CodingOperation(
        CodingWorkOrder(
            "job",
            "test/repo",
            "repo",
            "v1",
            "a" * 64,
            "b" * 40,
            "c" * 64,
            "Implement issue",
            "codex",
            "account",
            100,
            200,
            30,
        )
    )
    contract = ExecutionContract(
        "job",
        "Implement issue",
        "Repository",
        (),
        {},
        "implementer",
        ("/workspace",),
        "Capture edits",
        (),
        "Validate",
        "/workspace",
        {},
        "standard",
        operation=operation,
    )
    return RunRecord(
        job_id="job",
        node_id="worker",
        workspace_id="workspace",
        reservation_id="reservation",
        allocation_id="allocation",
        driver="remote-coding",
        backend="remote",
        contract=contract,
        handle=RunHandle("run", "remote-coding"),
        state=RunState.FAILED,
        result=RunResult(
            RunOutcome.FAILED,
            usage=TokenUsage(),
            metadata={
                "preparation_failure": True,
                "provider_started": False,
                "telemetry_valid": True,
            },
        ),
    )


def test_proven_preparation_attempts_leave_coding_budget_without_erasing_runs():
    preparation = preparation_run()
    coding = replace(
        preparation,
        state=RunState.COMPLETED,
        result=RunResult(
            RunOutcome.COMPLETED,
            usage=TokenUsage(input_tokens=12),
            consumed_quota=12,
            metadata={"telemetry_valid": True},
        ),
    )
    runs = [preparation] * 4 + [coding]
    original = runs.copy()
    limits = CodingAttemptLimits(3, 4, 7)
    counts = limits.count(iter(runs))
    assert (counts.coding, counts.preparation, counts.total) == (1, 4, 5)
    assert limits.blocked_reason(runs) is None
    assert "preparation attempt limit" in limits.blocked_reason(
        runs, preparation_retry=True
    )
    assert runs == original


def test_total_cap_stops_repair_even_with_coding_budget_available():
    run = preparation_run()
    runs = [run] * 4 + [replace(run, result=None)]
    assert "total attempt limit" in CodingAttemptLimits(3, 4, 5).blocked_reason(runs)


def test_unknown_and_zero_token_provider_failures_consume_coding_attempts():
    run = preparation_run()
    provider = replace(
        run,
        result=replace(
            run.result,
            metadata={**run.result.metadata, "provider_started": True},
        ),
    )
    runs = [replace(run, result=None), provider, run]
    counts = CodingAttemptLimits(2, 3).count(runs)
    assert (counts.coding, counts.preparation, counts.total) == (2, 1, 3)
    assert "provider attempt limit" in CodingAttemptLimits(2, 3).blocked_reason(runs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("preparation_failure", None),
        ("preparation_failure", False),
        ("preparation_failure", 1),
        ("provider_started", None),
        ("provider_started", True),
        ("provider_started", 0),
        ("telemetry_valid", None),
        ("telemetry_valid", False),
        ("telemetry_valid", 1),
    ],
)
def test_incomplete_or_untyped_proof_counts_as_coding(field, value):
    run = preparation_run()
    result = replace(run.result, metadata={**run.result.metadata, field: value})
    run = replace(run, result=result)
    assert not is_proven_preparation_failure(run)
    assert CodingAttemptLimits().count([run]).coding == 1


@pytest.mark.parametrize(
    "change",
    [
        {"state": RunState.STARTING},
        {"state": RunState.COMPLETED},
        {"state": RunState.CANCELLED},
        {"backend": "direct"},
        {"driver": "codex"},
        {"driver": "unknown"},
        {"result": None},
    ],
)
def test_unstopped_or_local_execution_counts_as_coding(change):
    assert not is_proven_preparation_failure(replace(preparation_run(), **change))


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": RunOutcome.COMPLETED},
        {"usage": None},
        {"usage": TokenUsage(input_tokens=1)},
        {"usage": TokenUsage(output_tokens=1)},
        {"consumed_quota": 1},
    ],
)
def test_contradictory_usage_or_outcome_counts_as_coding(change):
    run = preparation_run()
    assert not is_proven_preparation_failure(
        replace(run, result=replace(run.result, **change))
    )


def test_non_coding_contract_is_never_excluded():
    run = preparation_run()
    assert not is_proven_preparation_failure(
        replace(run, contract=replace(run.contract, operation=None))
    )


@pytest.mark.parametrize(
    "values",
    [
        {"maximum_coding_attempts": True},
        {"maximum_coding_attempts": 0},
        {"maximum_coding_attempts": 101},
        {"maximum_preparation_attempts": True},
        {"maximum_preparation_attempts": 0},
        {"maximum_preparation_attempts": 101},
        {"maximum_total_attempts": True},
        {"maximum_total_attempts": 0},
        {"maximum_total_attempts": 201},
    ],
)
def test_attempt_limits_reject_unbounded_or_boolean_values(values):
    with pytest.raises(ValueError, match="bounded"):
        CodingAttemptLimits(**values)


def test_default_total_cap_is_sum_of_separate_limits():
    assert CodingAttemptLimits(3, 4).maximum_total_attempts == 7
    assert CodingAttemptLimits(100, 100).maximum_total_attempts == 200
