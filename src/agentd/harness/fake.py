"""Deterministic harness implementation for scheduler and lifecycle tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agentd.domain.enums import RunOutcome
from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    RunHandle,
    RunResult,
    new_id,
)
from agentd.harness.errors import RunNotActiveError, UnknownRunError


@dataclass(frozen=True, slots=True)
class FakeHarnessCall:
    """One observable operation performed against a fake run."""

    operation: str
    instruction: str | None = None


@dataclass(slots=True)
class _FakeRun:
    execution: ExecutionContract
    result: RunResult
    calls: list[FakeHarnessCall]
    cancelled: bool = False
    collected_result: RunResult | None = None


class FakeHarnessDriver:
    """A no-I/O driver with configurable results and per-run call logs."""

    def __init__(
        self,
        *,
        capabilities: HarnessCapabilities | None = None,
        result: RunResult | None = None,
        result_factory: Callable[[ExecutionContract], RunResult] | None = None,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        if result is not None and result_factory is not None:
            raise ValueError("Configure either result or result_factory, not both")
        self._capabilities = capabilities or HarnessCapabilities(
            name="fake",
            models=frozenset({"standard"}),
            features=frozenset({"checkpointing", "steering"}),
            native_pause=True,
            steering=True,
            checkpointing=True,
        )
        default_result = result or RunResult(
            outcome=RunOutcome.COMPLETED,
            summary="fake harness completed",
        )
        self._result_factory = result_factory or (lambda _execution: default_result)
        self._id_factory = id_factory
        self._runs: dict[str, _FakeRun] = {}

    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    async def start(self, execution: ExecutionContract) -> RunHandle:
        run_id = self._id_factory()
        handle = RunHandle(
            id=run_id,
            driver=self._capabilities.name,
            external_id=f"fake:{run_id}",
        )
        self._runs[run_id] = _FakeRun(
            execution=execution,
            result=self._result_factory(execution),
            calls=[FakeHarnessCall("start")],
        )
        return handle

    async def steer(self, run: RunHandle, instruction: str) -> None:
        state = self._active_state(run)
        if not instruction.strip():
            raise ValueError("A steering instruction cannot be empty")
        state.calls.append(FakeHarnessCall("steer", instruction))

    async def interrupt(self, run: RunHandle) -> None:
        state = self._state(run)
        if state.cancelled or state.collected_result is not None:
            return
        state.calls.append(FakeHarnessCall("interrupt"))

    async def collect(self, run: RunHandle) -> RunResult:
        state = self._state(run)
        state.calls.append(FakeHarnessCall("collect"))
        if state.collected_result is None:
            if state.cancelled:
                state.collected_result = RunResult(
                    outcome=RunOutcome.CANCELLED,
                    summary="fake harness cancelled",
                )
            else:
                state.collected_result = state.result
        return state.collected_result

    async def cancel(self, run: RunHandle) -> None:
        state = self._state(run)
        state.calls.append(FakeHarnessCall("cancel"))
        state.cancelled = True

    def calls_for(self, run: RunHandle) -> tuple[FakeHarnessCall, ...]:
        return tuple(self._state(run).calls)

    def execution_for(self, run: RunHandle) -> ExecutionContract:
        return self._state(run).execution

    def configure_result(self, run: RunHandle, result: RunResult) -> None:
        state = self._active_state(run)
        state.result = result

    def _active_state(self, run: RunHandle) -> _FakeRun:
        state = self._state(run)
        if state.cancelled or state.collected_result is not None:
            raise RunNotActiveError(f"Fake harness run {run.id!r} is not active")
        return state

    def _state(self, run: RunHandle) -> _FakeRun:
        if run.driver != self._capabilities.name:
            raise UnknownRunError(
                f"Run {run.id!r} belongs to driver {run.driver!r}, "
                f"not {self._capabilities.name!r}"
            )
        try:
            return self._runs[run.id]
        except KeyError as error:
            raise UnknownRunError(f"Unknown fake harness run {run.id!r}") from error
