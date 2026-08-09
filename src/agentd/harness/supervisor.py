"""Durable ownership of streamed Codex App Server runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from agentd.domain.enums import QuotaUnit, RunOutcome, RunState
from agentd.domain.models import (
    DriverSession,
    ExecutionContract,
    JsonValue,
    RunCommand,
    RunCommandAck,
    RunHandle,
    RunObservation,
    RunRecord,
    RunResult,
    TokenUsage,
    UsageApplication,
    UsageSample,
    utc_now,
)
from agentd.harness.app_server import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_REASONING_EFFORT,
    TERMINAL_EVENT_METHODS,
    AppServerClient,
    AppServerEvent,
    OpenAICodexClient,
)
from agentd.harness.codex import render_execution_contract
from agentd.harness.errors import RunNotActiveError, UnknownRunError
from agentd.state.base import ConcurrentStateError, EntityNotFoundError

CODEX_DRIVER_NAME = "codex"
CODEX_RESULT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "completed": {"type": "array", "items": {"type": "string"}},
        "current": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "known_failures": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "commit": {"type": ["string", "null"]},
        "review_requested": {"type": "boolean"},
    },
    "required": [
        "summary",
        "completed",
        "current",
        "next_steps",
        "known_failures",
        "decisions",
        "commit",
        "review_requested",
    ],
    "additionalProperties": False,
}
DEFAULT_RECOVERY_INSTRUCTION = "Resume from the durable thread and workspace."


@runtime_checkable
class RunSupervisorStore(Protocol):
    """Durable store surface required by :class:`RunSupervisor`."""

    def get_run(self, run_id: str) -> RunRecord: ...

    def save_driver_session(self, session: DriverSession) -> None: ...

    def get_driver_session(self, run_id: str) -> DriverSession: ...

    def list_driver_sessions(
        self, active: bool | None = None
    ) -> list[DriverSession]: ...

    def update_observation_cursor(
        self,
        run_id: str,
        expected_cursor: str | None,
        cursor: str,
        observation: RunObservation | None = None,
    ) -> DriverSession: ...

    def apply_usage_sample(
        self,
        sample: UsageSample,
        *,
        maximum: float | None = None,
    ) -> UsageApplication: ...

    def list_usage_samples(self, run_id: str) -> list[UsageSample]: ...

    def enqueue_run_command(self, command: RunCommand) -> None: ...

    def list_pending_run_commands(
        self, run_id: str | None = None
    ) -> list[RunCommand]: ...

    def acknowledge_run_command(self, acknowledgement: RunCommandAck) -> None: ...


AppServerClientFactory = Callable[[ExecutionContract], AppServerClient]


def _default_client_factory(execution: ExecutionContract) -> AppServerClient:
    return OpenAICodexClient(environment=execution.environment)


@dataclass(slots=True)
class _LiveRun:
    execution: ExecutionContract
    client: AppServerClient
    thread_id: str
    turn_id: str
    sequence: int
    result_future: asyncio.Future[RunResult]
    thread_usage_baseline: TokenUsage
    task: asyncio.Task[None] | None = None
    final_response: str = ""
    current_turn_usage: TokenUsage | None = None
    telemetry_valid: bool = True
    telemetry_error: str | None = None
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunSupervisor:
    """Own App Server streams while persisting enough state for recovery.

    App Server's stdio transport cannot be reattached after a controller crash.
    Recovery therefore resumes the durable Codex thread and starts a fresh turn
    in the still-owned workspace.  The previous in-flight process is never
    signalled by a stale PID.
    """

    def __init__(
        self,
        store: RunSupervisorStore,
        *,
        client_factory: AppServerClientFactory = _default_client_factory,
        model: str = DEFAULT_CODEX_MODEL,
        effort: str = DEFAULT_REASONING_EFFORT,
        output_schema: Mapping[str, JsonValue] = CODEX_RESULT_SCHEMA,
        toolchain_read_roots: Sequence[str] = (),
    ) -> None:
        if model != DEFAULT_CODEX_MODEL:
            raise ValueError(
                f"Codex SDK driver model is fixed to {DEFAULT_CODEX_MODEL!r}"
            )
        if effort != DEFAULT_REASONING_EFFORT:
            raise ValueError(
                "Codex SDK driver reasoning effort is fixed to "
                f"{DEFAULT_REASONING_EFFORT!r}"
            )
        self._store = store
        self._client_factory = client_factory
        self._model = model
        self._effort = effort
        self._output_schema = dict(output_schema)
        self._toolchain_read_roots = _validate_toolchain_roots(toolchain_read_roots)
        self._live: dict[str, _LiveRun] = {}
        self._closed = False

    async def start(
        self,
        run_id: str,
        execution: ExecutionContract,
    ) -> RunHandle:
        """Start and durably identify a new SDK thread and streamed turn."""

        self._require_open()
        if not run_id.strip():
            raise ValueError("A managed Codex run requires a run identifier")
        try:
            existing = self._store.get_driver_session(run_id)
        except EntityNotFoundError:
            existing = None
        if existing is not None:
            raise RuntimeError(f"Codex run {run_id!r} already has a driver session")

        workspace = _validate_execution_scope(execution)
        client = self._client_factory(execution)
        await client.start()
        try:
            thread_id = await client.start_thread(
                cwd=str(workspace),
                model=self._model,
            )
            metadata = _session_metadata(client, recovered=False)
            session = DriverSession(
                run_id=run_id,
                driver=CODEX_DRIVER_NAME,
                external_id=thread_id,
                thread_id=thread_id,
                metadata=metadata,
            )
            self._store.save_driver_session(session)
            turn_id = await self._start_turn(
                client,
                thread_id,
                render_execution_contract(execution),
                workspace,
            )
            session = replace(session, turn_id=turn_id, updated_at=utc_now())
            self._store.save_driver_session(session)
            live = self._activate(
                run_id,
                execution,
                client,
                thread_id,
                turn_id,
                session,
            )
        except BaseException:
            await client.close()
            raise

        live.task = asyncio.create_task(
            self._consume(run_id, live),
            name=f"agentd-codex-{run_id}",
        )
        return RunHandle(
            id=run_id,
            driver=CODEX_DRIVER_NAME,
            external_id=thread_id,
        )

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = DEFAULT_RECOVERY_INSTRUCTION,
    ) -> RunHandle:
        """Resume a durable thread after this process lost its live transport."""

        self._require_open()
        if not recovery_instruction.strip():
            raise ValueError("A recovery instruction cannot be empty")
        live = self._live.get(run_id)
        if live is not None:
            return RunHandle(
                id=run_id,
                driver=CODEX_DRIVER_NAME,
                external_id=live.thread_id,
            )
        session = self._session(run_id)
        self._validate_session(session)
        if not session.active:
            return RunHandle(
                id=run_id,
                driver=CODEX_DRIVER_NAME,
                external_id=session.thread_id,
            )
        if session.thread_id is None:
            raise RunNotActiveError(
                f"Codex run {run_id!r} has no durable thread to recover"
            )

        workspace = _validate_execution_scope(execution)
        client = self._client_factory(execution)
        await client.start()
        try:
            thread_id = await client.resume_thread(
                session.thread_id,
                cwd=str(workspace),
                model=self._model,
            )
            prompt = (
                f"{recovery_instruction.strip()}\n\n"
                "Current bounded assignment:\n"
                f"{render_execution_contract(execution)}"
            )
            turn_id = await self._start_turn(
                client,
                thread_id,
                prompt,
                workspace,
            )
            recovery_count = _metadata_int(session.metadata, "recovery_count") + 1
            session = replace(
                session,
                external_id=thread_id,
                thread_id=thread_id,
                turn_id=turn_id,
                active=True,
                metadata={
                    **session.metadata,
                    **_session_metadata(client, recovered=True),
                    "recovery_count": recovery_count,
                },
                updated_at=utc_now(),
            )
            self._store.save_driver_session(session)
            live = self._activate(
                run_id,
                execution,
                client,
                thread_id,
                turn_id,
                session,
            )
        except BaseException:
            await client.close()
            raise

        try:
            await self._deliver_pending(run_id, live)
        except BaseException:
            self._live.pop(run_id, None)
            await client.close()
            raise
        live.task = asyncio.create_task(
            self._consume(run_id, live),
            name=f"agentd-codex-recovery-{run_id}",
        )
        return RunHandle(
            id=run_id,
            driver=CODEX_DRIVER_NAME,
            external_id=thread_id,
        )

    async def continue_turn(
        self,
        run_id: str,
        instruction: str,
    ) -> RunHandle:
        """Start a bounded follow-up turn on a completed durable thread."""

        self._require_open()
        if not instruction.strip():
            raise ValueError("A continuation instruction cannot be empty")
        if run_id in self._live:
            raise RunNotActiveError(f"Codex run {run_id!r} still has an active turn")
        session = self._session(run_id)
        self._validate_session(session)
        observation = session.last_observation
        if session.active or observation is None or not observation.terminal:
            raise RunNotActiveError(
                f"Codex run {run_id!r} has not reached a terminal turn boundary"
            )
        if not observation.telemetry_valid:
            raise RunNotActiveError(
                f"Codex run {run_id!r} has unresolved terminal telemetry"
            )
        if session.thread_id is None:
            raise RunNotActiveError(
                f"Codex run {run_id!r} has no durable thread to continue"
            )

        execution = self._store.get_run(run_id).contract
        workspace = _validate_execution_scope(execution)
        client = self._client_factory(execution)
        await client.start()
        try:
            thread_id = await client.resume_thread(
                session.thread_id,
                cwd=str(workspace),
                model=self._model,
            )
            prompt = (
                f"{instruction.strip()}\n\n"
                "Current bounded assignment:\n"
                f"{render_execution_contract(execution)}"
            )
            turn_id = await self._start_turn(
                client,
                thread_id,
                prompt,
                workspace,
            )
            continuation_count = (
                _metadata_int(session.metadata, "continuation_count") + 1
            )
            session = replace(
                session,
                external_id=thread_id,
                thread_id=thread_id,
                turn_id=turn_id,
                active=True,
                metadata={
                    **session.metadata,
                    **_session_metadata(client, recovered=False),
                    "continued": True,
                    "continuation_count": continuation_count,
                },
                updated_at=utc_now(),
            )
            self._store.save_driver_session(session)
            live = self._activate(
                run_id,
                execution,
                client,
                thread_id,
                turn_id,
                session,
            )
            await self._deliver_pending(run_id, live)
        except BaseException:
            self._live.pop(run_id, None)
            await client.close()
            raise

        live.task = asyncio.create_task(
            self._consume(run_id, live),
            name=f"agentd-codex-continuation-{run_id}",
        )
        return RunHandle(
            id=run_id,
            driver=CODEX_DRIVER_NAME,
            external_id=thread_id,
        )

    def observe(self, run_id: str) -> RunObservation | None:
        """Return the latest durable observation without waiting for the stream."""

        try:
            session = self._store.get_driver_session(run_id)
        except EntityNotFoundError:
            return None
        self._validate_session(session)
        return session.last_observation

    def active(self) -> tuple[RunObservation, ...]:
        """List durable active observations in deterministic run order."""

        observations = (
            session.last_observation
            for session in self._store.list_driver_sessions(active=True)
            if session.driver == CODEX_DRIVER_NAME
        )
        return tuple(
            sorted(
                (item for item in observations if item is not None),
                key=lambda item: item.run_id,
            )
        )

    async def collect(self, run_id: str) -> RunResult:
        """Wait for a live result or return an already-persisted terminal result."""

        observation = self.observe(run_id)
        if observation is not None and observation.terminal:
            if observation.result is None:
                raise RuntimeError(f"Terminal Codex run {run_id!r} has no result")
            return observation.result
        live = self._live.get(run_id)
        if live is None:
            session = self._session(run_id)
            if session.active:
                raise RunNotActiveError(
                    f"Codex run {run_id!r} lost its live transport; recover it first"
                )
            raise RuntimeError(f"Codex run {run_id!r} ended without a result")
        return await asyncio.shield(live.result_future)

    async def steer(self, run_id: str, instruction: str) -> None:
        if not instruction.strip():
            raise ValueError("A steering instruction cannot be empty")
        await self._queue_command(
            RunCommand(
                run_id=run_id,
                action="steer",
                payload={"instruction": instruction},
            )
        )

    async def interrupt(self, run_id: str) -> None:
        await self._queue_command(RunCommand(run_id=run_id, action="interrupt"))

    async def cancel(self, run_id: str) -> None:
        await self._queue_command(RunCommand(run_id=run_id, action="cancel"))

    async def process_pending(self, run_id: str) -> None:
        """Deliver durable unacknowledged commands to a live session."""

        session = self._session(run_id)
        self._validate_session(session)
        if not session.active:
            raise RunNotActiveError(f"Codex run {run_id!r} is not active")
        live = self._live.get(run_id)
        if live is None:
            raise RunNotActiveError(
                f"Codex run {run_id!r} has no live transport; recover it first"
            )
        await self._deliver_pending(run_id, live)

    async def close(self) -> None:
        """Close transports without declaring durable active sessions terminal."""

        if self._closed:
            return
        self._closed = True
        live_runs = tuple(self._live.values())
        for live in live_runs:
            if live.task is not None:
                live.task.cancel()
        if live_runs:
            await asyncio.gather(
                *(live.task for live in live_runs if live.task is not None),
                return_exceptions=True,
            )
        self._live.clear()

    async def _start_turn(
        self,
        client: AppServerClient,
        thread_id: str,
        prompt: str,
        workspace: Path,
    ) -> str:
        readable = (str(workspace), *self._toolchain_read_roots)
        return await client.start_turn(
            thread_id,
            prompt,
            cwd=str(workspace),
            model=self._model,
            effort=self._effort,
            output_schema=self._output_schema,
            writable_roots=(str(workspace),),
            readable_roots=readable,
        )

    def _activate(
        self,
        run_id: str,
        execution: ExecutionContract,
        client: AppServerClient,
        thread_id: str,
        turn_id: str,
        session: DriverSession,
    ) -> _LiveRun:
        sequence = _cursor_sequence(session.observation_cursor)
        initial = RunObservation(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            cursor=str(sequence + 1),
            terminal=False,
            telemetry_valid=True,
            unit=QuotaUnit.TOKENS,
            source="codex-app-server",
            run_state=RunState.RUNNING,
            provider_epoch=f"{thread_id}:{turn_id}",
            metadata=_observation_metadata(client, "turn/started"),
        )
        updated = self._store.update_observation_cursor(
            run_id,
            session.observation_cursor,
            initial.cursor,
            initial,
        )
        loop = asyncio.get_running_loop()
        live = _LiveRun(
            execution=execution,
            client=client,
            thread_id=thread_id,
            turn_id=turn_id,
            sequence=_cursor_sequence(updated.observation_cursor),
            result_future=loop.create_future(),
            thread_usage_baseline=_thread_usage_total(
                self._store.list_usage_samples(run_id),
                thread_id,
                excluding_turn_id=turn_id,
            ),
        )
        self._live[run_id] = live
        return live

    async def _consume(self, run_id: str, live: _LiveRun) -> None:
        try:
            terminal_received = False
            async for event in live.client.events(live.turn_id):
                await self._consume_event(run_id, live, event)
                if event.method in TERMINAL_EVENT_METHODS:
                    terminal_received = True
                    break
            if not terminal_received:
                raise RuntimeError(
                    "Codex App Server stream ended without a terminal event"
                )
        except asyncio.CancelledError:
            if not live.result_future.done():
                live.result_future.cancel()
            raise
        except BaseException as error:
            try:
                live.sequence += 1
                live.telemetry_valid = False
                live.telemetry_error = (
                    f"stream ended before terminal telemetry was verified: {error}"
                )
                result = self._failure_result(run_id, live, error)
                await self._persist_terminal(
                    run_id,
                    live,
                    result,
                    method="stream/error",
                )
            except BaseException as persistence_error:
                if not live.result_future.done():
                    live.result_future.set_exception(persistence_error)
            else:
                if not live.result_future.done():
                    live.result_future.set_result(result)
        finally:
            await live.client.close()
            if self._live.get(run_id) is live:
                self._live.pop(run_id, None)

    async def _consume_event(
        self,
        run_id: str,
        live: _LiveRun,
        event: AppServerEvent,
    ) -> None:
        live.sequence += 1
        if event.method == "thread/tokenUsage/updated":
            self._consume_usage(run_id, live, event)
        elif event.method == "item/completed":
            response = _agent_message(event.payload)
            if response is not None:
                live.final_response = response
        elif event.method == "item/agentMessage/delta":
            delta = event.payload.get("delta")
            if isinstance(delta, str):
                live.final_response += delta

        if event.method in TERMINAL_EVENT_METHODS:
            self._mark_terminal_telemetry(live)
            self._finalize_usage(run_id, live)
            result = self._terminal_result(
                run_id,
                live,
                event.method,
                event.payload,
            )
            await self._persist_terminal(
                run_id,
                live,
                result,
                method=event.method,
            )
            if not live.result_future.done():
                live.result_future.set_result(result)
            return

        observation = self._observation(
            run_id,
            live,
            event.method,
            terminal=False,
            result=None,
            run_state=RunState.RUNNING,
        )
        self._advance(run_id, live, observation)

    def _consume_usage(
        self,
        run_id: str,
        live: _LiveRun,
        event: AppServerEvent,
    ) -> None:
        try:
            if event.payload.get("threadId") != live.thread_id:
                raise ValueError("Codex token telemetry belongs to another thread")
            if event.payload.get("turnId") != live.turn_id:
                raise ValueError("Codex token telemetry belongs to another turn")
            provider_total = _token_usage(event.payload)
            usage = _subtract_usage(provider_total, live.thread_usage_baseline)
            previous = live.current_turn_usage
            if previous is not None and not usage.dominates(previous):
                raise ValueError("Codex token telemetry moved backwards")
            live.current_turn_usage = usage
            if usage.total_tokens == 0 or (
                previous is not None and usage.total_tokens == previous.total_tokens
            ):
                return
            sample = UsageSample(
                run_id=run_id,
                thread_id=live.thread_id,
                turn_id=live.turn_id,
                sequence=live.sequence,
                cumulative_quota=float(usage.total_tokens),
                unit=QuotaUnit.TOKENS,
                source="codex-app-server",
                tokens=usage,
                provider_epoch=f"{live.thread_id}:{live.turn_id}",
                metadata={
                    "model": self._model,
                    "effort": self._effort,
                    "sdk_version": live.client.metadata.sdk_version,
                    "runtime_version": live.client.metadata.runtime_version,
                },
            )
            self._store.apply_usage_sample(sample)
        except (ConcurrentStateError, TypeError, ValueError) as error:
            live.telemetry_valid = False
            live.telemetry_error = str(error)

    def _finalize_usage(self, run_id: str, live: _LiveRun) -> None:
        """Persist a distinct terminal marker without charging twice."""

        usage = live.current_turn_usage
        if usage is None or not live.telemetry_valid:
            return
        try:
            self._store.apply_usage_sample(
                UsageSample(
                    run_id=run_id,
                    thread_id=live.thread_id,
                    turn_id=live.turn_id,
                    sequence=live.sequence,
                    cumulative_quota=float(usage.total_tokens),
                    unit=QuotaUnit.TOKENS,
                    source="codex-app-server",
                    tokens=usage,
                    provider_epoch=f"{live.thread_id}:{live.turn_id}",
                    final=True,
                    metadata={
                        "model": self._model,
                        "effort": self._effort,
                        "sdk_version": live.client.metadata.sdk_version,
                        "runtime_version": live.client.metadata.runtime_version,
                    },
                )
            )
        except (
            ConcurrentStateError,
            EntityNotFoundError,
            TypeError,
            ValueError,
        ) as error:
            live.telemetry_valid = False
            live.telemetry_error = str(error)

    async def _persist_terminal(
        self,
        run_id: str,
        live: _LiveRun,
        result: RunResult,
        *,
        method: str,
    ) -> None:
        observation = self._observation(
            run_id,
            live,
            method,
            terminal=True,
            result=result,
            run_state=_outcome_state(result.outcome),
        )
        self._advance(run_id, live, observation)

    def _terminal_result(
        self,
        run_id: str,
        live: _LiveRun,
        method: str,
        payload: Mapping[str, JsonValue],
    ) -> RunResult:
        turn = _mapping(payload.get("turn"))
        status = turn.get("status")
        error = _mapping(turn.get("error"))
        error_message = error.get("message") or payload.get("message")
        if method == "turn/completed" and status == "completed":
            outcome = RunOutcome.COMPLETED
        elif method == "turn/completed" and status == "interrupted":
            outcome = RunOutcome.CANCELLED
        else:
            outcome = RunOutcome.FAILED

        structured, structured_error = _structured_result(live.final_response)
        summary = _optional_string(structured.get("summary"))
        if outcome is RunOutcome.FAILED and isinstance(error_message, str):
            summary = error_message
        if not summary:
            summary = live.final_response or f"Codex turn {status or 'failed'}"
        commit = structured.get("commit")
        if not isinstance(commit, str):
            commit = None
        metadata: dict[str, JsonValue] = {
            "thread_id": live.thread_id,
            "turn_id": live.turn_id,
            "model": self._model,
            "effort": self._effort,
            "sdk_version": live.client.metadata.sdk_version,
            "runtime_version": live.client.metadata.runtime_version,
            "structured_output_valid": structured_error is None,
            "completed": structured.get("completed"),
            "current": structured.get("current"),
            "next_steps": structured.get("next_steps"),
            "known_failures": structured.get("known_failures"),
            "decisions": structured.get("decisions"),
            "review_requested": structured.get("review_requested"),
            "telemetry_valid": live.telemetry_valid,
            "observation_cursor": str(live.sequence),
        }
        if structured_error is not None:
            metadata["structured_output_error"] = structured_error
        if live.telemetry_error is not None:
            metadata["telemetry_error"] = live.telemetry_error
        usage = _aggregate_usage(
            self._store.list_usage_samples(run_id),
            live.thread_id,
            live.turn_id,
            live.current_turn_usage,
        )
        return RunResult(
            outcome=outcome,
            summary=summary,
            commit=commit,
            consumed_quota=0,
            metadata=metadata,
            usage=usage,
        )

    def _failure_result(
        self, run_id: str, live: _LiveRun, error: BaseException
    ) -> RunResult:
        usage = _aggregate_usage(
            self._store.list_usage_samples(run_id),
            live.thread_id,
            live.turn_id,
            live.current_turn_usage,
        )
        return RunResult(
            outcome=RunOutcome.FAILED,
            summary=f"Codex App Server stream failed: {error}",
            consumed_quota=0,
            usage=usage,
            metadata={
                "thread_id": live.thread_id,
                "turn_id": live.turn_id,
                "model": self._model,
                "effort": self._effort,
                "sdk_version": live.client.metadata.sdk_version,
                "runtime_version": live.client.metadata.runtime_version,
                "stream_error": str(error),
                "telemetry_valid": False,
            },
        )

    @staticmethod
    def _mark_terminal_telemetry(live: _LiveRun) -> None:
        if live.current_turn_usage is None:
            live.telemetry_valid = False
            live.telemetry_error = live.telemetry_error or "no token usage was reported"

    def _observation(
        self,
        run_id: str,
        live: _LiveRun,
        method: str,
        *,
        terminal: bool,
        result: RunResult | None,
        run_state: RunState,
    ) -> RunObservation:
        metadata = _observation_metadata(live.client, method)
        if live.telemetry_error is not None:
            metadata["telemetry_error"] = live.telemetry_error
        return RunObservation(
            run_id=run_id,
            thread_id=live.thread_id,
            turn_id=live.turn_id,
            cursor=str(live.sequence),
            terminal=terminal,
            telemetry_valid=live.telemetry_valid,
            usage=live.current_turn_usage,
            cumulative_quota=(
                float(live.current_turn_usage.total_tokens)
                if live.current_turn_usage is not None
                else None
            ),
            unit=QuotaUnit.TOKENS,
            source="codex-app-server",
            run_state=run_state,
            result=result,
            provider_epoch=f"{live.thread_id}:{live.turn_id}",
            metadata=metadata,
        )

    def _advance(
        self,
        run_id: str,
        live: _LiveRun,
        observation: RunObservation,
    ) -> None:
        expected = str(live.sequence - 1)
        self._store.update_observation_cursor(
            run_id,
            expected,
            observation.cursor,
            observation,
        )

    async def _queue_command(self, command: RunCommand) -> None:
        session = self._session(command.run_id)
        self._validate_session(session)
        if not session.active:
            raise RunNotActiveError(f"Codex run {command.run_id!r} is not active")
        self._store.enqueue_run_command(command)
        live = self._live.get(command.run_id)
        if live is not None:
            await self._deliver_pending(command.run_id, live)

    async def _deliver_pending(self, run_id: str, live: _LiveRun) -> None:
        async with live.command_lock:
            for command in self._store.list_pending_run_commands(run_id):
                if command.action == "repair":
                    # Repair requests are consumed by the scheduler, which
                    # owns reservations, placement, and the two-turn limit.
                    continue
                await self._deliver_command(command, live)

    async def _deliver_command(self, command: RunCommand, live: _LiveRun) -> None:
        if command.action in {"steer", "checkpoint", "suspend"}:
            instruction = _command_instruction(command)
            await live.client.steer(
                live.thread_id,
                live.turn_id,
                f"{instruction}\n\nControl-plane command id: {command.id}",
            )
        elif command.action in {"interrupt", "cancel"}:
            await live.client.interrupt(live.thread_id, live.turn_id)
        else:
            raise ValueError(f"Unsupported Codex run command: {command.action!r}")
        self._store.acknowledge_run_command(
            RunCommandAck(
                command_id=command.id,
                run_id=command.run_id,
                observation_cursor=str(live.sequence),
                metadata={"thread_id": live.thread_id, "turn_id": live.turn_id},
            )
        )

    def _session(self, run_id: str) -> DriverSession:
        try:
            return self._store.get_driver_session(run_id)
        except EntityNotFoundError as error:
            raise UnknownRunError(f"Unknown Codex run {run_id!r}") from error

    @staticmethod
    def _validate_session(session: DriverSession) -> None:
        if session.driver != CODEX_DRIVER_NAME:
            raise UnknownRunError(
                f"Run {session.run_id!r} belongs to driver {session.driver!r}, "
                f"not {CODEX_DRIVER_NAME!r}"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Codex run supervisor is closed")


def _validate_execution_scope(execution: ExecutionContract) -> Path:
    workspace = Path(execution.working_directory).expanduser()
    if not workspace.is_absolute():
        raise ValueError("Codex SDK working directory must be absolute")
    workspace = workspace.resolve()
    codex_state = (Path.home() / ".codex").resolve()
    if _paths_overlap(workspace, codex_state):
        raise ValueError("Codex SDK workspace cannot expose Codex account state")
    for raw_scope in execution.allowed_filesystem_scope:
        scope = Path(raw_scope).expanduser()
        if not scope.is_absolute():
            raise ValueError("Codex SDK filesystem scopes must be absolute")
        resolved = scope.resolve()
        if resolved != workspace and not resolved.is_relative_to(workspace):
            raise ValueError(
                "Codex SDK writable scope must remain inside the leased worktree: "
                f"{raw_scope}"
            )
    return workspace


def _validate_toolchain_roots(roots: Sequence[str]) -> tuple[str, ...]:
    codex_state = (Path.home() / ".codex").resolve()
    validated: list[str] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise ValueError("Toolchain read roots must be absolute")
        resolved = root.resolve()
        if _paths_overlap(resolved, codex_state):
            raise ValueError("Toolchain read roots cannot expose Codex account state")
        text = str(resolved)
        if text not in validated:
            validated.append(text)
    return tuple(validated)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _session_metadata(
    client: AppServerClient, *, recovered: bool
) -> dict[str, JsonValue]:
    metadata = client.metadata
    return {
        "sdk_version": metadata.sdk_version,
        "runtime_version": metadata.runtime_version,
        "server_name": metadata.server_name,
        "user_agent": metadata.user_agent,
        "platform_family": metadata.platform_family,
        "platform_os": metadata.platform_os,
        "model": DEFAULT_CODEX_MODEL,
        "effort": DEFAULT_REASONING_EFFORT,
        "sandbox": "restricted-workspace-write",
        "recovered": recovered,
    }


def _observation_metadata(client: AppServerClient, method: str) -> dict[str, JsonValue]:
    return {
        "event_method": method,
        "sdk_version": client.metadata.sdk_version,
        "runtime_version": client.metadata.runtime_version,
        "model": DEFAULT_CODEX_MODEL,
        "effort": DEFAULT_REASONING_EFFORT,
    }


def _cursor_sequence(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as error:
        raise ValueError(f"Invalid Codex observation cursor: {cursor!r}") from error
    if value < 0:
        raise ValueError("Codex observation cursor cannot be negative")
    return value


def _metadata_int(metadata: Mapping[str, JsonValue], key: str) -> int:
    value = metadata.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _optional_string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _agent_message(payload: Mapping[str, JsonValue]) -> str | None:
    item = _mapping(payload.get("item"))
    if item.get("type") not in {"agentMessage", "agent_message"}:
        return None
    text = item.get("text")
    return text if isinstance(text, str) else None


def _token_usage(payload: Mapping[str, JsonValue]) -> TokenUsage:
    token_usage = _mapping(payload.get("tokenUsage"))
    # ``last`` is the latest model/API call and can move backwards between tool
    # calls. ``total`` is monotonic for the durable thread; the supervisor
    # subtracts usage already persisted for earlier turns.
    usage = _mapping(token_usage.get("total"))
    parsed = TokenUsage(
        input_tokens=_required_counter(usage, "inputTokens"),
        cached_input_tokens=_required_counter(usage, "cachedInputTokens"),
        output_tokens=_required_counter(usage, "outputTokens"),
        reasoning_output_tokens=_required_counter(usage, "reasoningOutputTokens"),
    )
    if _required_counter(usage, "totalTokens") != parsed.total_tokens:
        raise ValueError("Codex total token telemetry is internally inconsistent")
    return parsed


def _required_counter(payload: Mapping[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Codex token counter {key!r} must be an integer")
    return value


def _subtract_usage(total: TokenUsage, baseline: TokenUsage) -> TokenUsage:
    if not total.dominates(baseline):
        raise ValueError("Codex thread token telemetry moved behind its baseline")
    return TokenUsage(
        input_tokens=total.input_tokens - baseline.input_tokens,
        cached_input_tokens=(total.cached_input_tokens - baseline.cached_input_tokens),
        output_tokens=total.output_tokens - baseline.output_tokens,
        reasoning_output_tokens=(
            total.reasoning_output_tokens - baseline.reasoning_output_tokens
        ),
    )


def _structured_result(response: str) -> tuple[Mapping[str, JsonValue], str | None]:
    if not response.strip():
        return {}, "Codex did not return a structured final response"
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as error:
        return {}, f"Codex returned invalid structured JSON: {error.msg}"
    if not isinstance(parsed, dict):
        return {}, "Codex structured response was not an object"
    summary = parsed.get("summary")
    commit = parsed.get("commit")
    list_fields = (
        "completed",
        "current",
        "next_steps",
        "known_failures",
        "decisions",
    )
    if not isinstance(summary, str):
        return cast(Mapping[str, JsonValue], parsed), "summary must be a string"
    if commit is not None and not isinstance(commit, str):
        return cast(Mapping[str, JsonValue], parsed), "commit must be a string or null"
    for field_name in list_fields:
        value = parsed.get(field_name)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return (
                cast(Mapping[str, JsonValue], parsed),
                f"{field_name} must be strings",
            )
    if not isinstance(parsed.get("review_requested"), bool):
        return cast(Mapping[str, JsonValue], parsed), "review_requested must be boolean"
    if set(parsed) != {
        "summary",
        "completed",
        "current",
        "next_steps",
        "known_failures",
        "decisions",
        "commit",
        "review_requested",
    }:
        return cast(Mapping[str, JsonValue], parsed), "unexpected structured fields"
    return cast(Mapping[str, JsonValue], parsed), None


def _aggregate_usage(
    samples: Sequence[UsageSample],
    current_thread_id: str,
    current_turn_id: str,
    current: TokenUsage | None,
) -> TokenUsage | None:
    latest: dict[tuple[str, str], UsageSample] = {}
    for sample in samples:
        key = (sample.thread_id, sample.turn_id)
        existing = latest.get(key)
        if existing is None or sample.sequence > existing.sequence:
            latest[key] = sample
    current_key = (current_thread_id, current_turn_id)
    usages = [
        sample.tokens
        for key, sample in latest.items()
        if key != current_key and sample.tokens is not None
    ]
    if current is not None:
        usages.append(current)
    if not usages:
        return None
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in usages),
        cached_input_tokens=sum(item.cached_input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in usages),
    )


def _thread_usage_total(
    samples: Sequence[UsageSample],
    thread_id: str,
    *,
    excluding_turn_id: str,
) -> TokenUsage:
    latest: dict[str, UsageSample] = {}
    for sample in samples:
        if sample.thread_id != thread_id or sample.turn_id == excluding_turn_id:
            continue
        existing = latest.get(sample.turn_id)
        if existing is None or sample.sequence > existing.sequence:
            latest[sample.turn_id] = sample
    usages = [sample.tokens for sample in latest.values() if sample.tokens is not None]
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in usages),
        cached_input_tokens=sum(item.cached_input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in usages),
    )


def _outcome_state(outcome: RunOutcome) -> RunState:
    if outcome is RunOutcome.COMPLETED:
        return RunState.COMPLETED
    if outcome is RunOutcome.CANCELLED:
        return RunState.CANCELLED
    return RunState.FAILED


def _command_instruction(command: RunCommand) -> str:
    supplied = command.payload.get("instruction")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    if command.action == "checkpoint":
        return (
            "Stop at the next safe boundary and return the structured durable "
            "checkpoint fields for completed work, current state, next steps, "
            "known failures, decisions, commit, and review request."
        )
    if command.action == "suspend":
        return (
            "Stop at the next safe boundary, do not begin more work, and return "
            "the structured durable state needed to resume later."
        )
    raise ValueError("A durable steer command requires an instruction")
