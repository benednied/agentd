import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from agentd.codex_versions import PINNED_OPENAI_CODEX_VERSION
from agentd.domain.enums import QuotaUnit, RunOutcome, RunState
from agentd.domain.models import (
    DriverSession,
    ExecutionContract,
    JsonValue,
    RunCommand,
    RunCommandAck,
    RunObservation,
    TokenUsage,
    UsageApplication,
    UsageSample,
)
from agentd.harness.app_server import (
    AppServerEvent,
    AppServerMetadata,
    OpenAICodexClient,
)
from agentd.harness.codex_sdk import CodexSdkDriver
from agentd.harness.native_record import NativeTerminalUsage
from agentd.harness.supervisor import RunSupervisor
from agentd.state.base import ConcurrentStateError, EntityNotFoundError


def test_driver_advertises_its_configured_model() -> None:
    driver = CodexSdkDriver(
        cast(RunSupervisor, object()),
        model="experimental-model",
    )

    assert "experimental-model" in driver.capabilities().models


@dataclass(frozen=True, slots=True)
class StoredRun:
    contract: ExecutionContract


@dataclass(slots=True)
class MemorySupervisorStore:
    sessions: dict[str, DriverSession] = field(default_factory=dict)
    samples: list[UsageSample] = field(default_factory=list)
    commands: list[RunCommand] = field(default_factory=list)
    acknowledgements: dict[str, RunCommandAck] = field(default_factory=dict)
    runs: dict[str, StoredRun] = field(default_factory=dict)

    def get_run(self, run_id: str) -> StoredRun:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise EntityNotFoundError(run_id) from error

    def save_driver_session(
        self,
        session: DriverSession,
        *,
        expected: DriverSession | None,
    ) -> None:
        existing = self.sessions.get(session.run_id)
        if expected is None:
            if existing is not None:
                raise ConcurrentStateError(session.run_id)
        elif existing != expected:
            raise ConcurrentStateError(session.run_id)
        self.sessions[session.run_id] = session

    def get_driver_session(self, run_id: str) -> DriverSession:
        try:
            return self.sessions[run_id]
        except KeyError as error:
            raise EntityNotFoundError(run_id) from error

    def list_driver_sessions(self, active: bool | None = None) -> list[DriverSession]:
        sessions = list(self.sessions.values())
        if active is not None:
            sessions = [session for session in sessions if session.active is active]
        return sorted(sessions, key=lambda session: session.run_id)

    def update_observation_cursor(
        self,
        run_id: str,
        expected_cursor: str | None,
        cursor: str,
        observation: RunObservation | None = None,
    ) -> DriverSession:
        session = self.get_driver_session(run_id)
        if session.observation_cursor != expected_cursor:
            raise ConcurrentStateError(run_id)
        updated = replace(
            session,
            observation_cursor=cursor,
            thread_id=(observation.thread_id if observation else session.thread_id),
            turn_id=(observation.turn_id if observation else session.turn_id),
            last_observation=observation or session.last_observation,
            active=(not observation.terminal if observation else session.active),
        )
        self.sessions[run_id] = updated
        return updated

    def apply_usage_sample(
        self,
        sample: UsageSample,
        *,
        maximum: float | None = None,
    ) -> UsageApplication:
        identity = (sample.run_id, sample.thread_id, sample.turn_id, sample.sequence)
        for existing in self.samples:
            existing_identity = (
                existing.run_id,
                existing.thread_id,
                existing.turn_id,
                existing.sequence,
            )
            if existing_identity == identity:
                assert existing == sample
                return cast(UsageApplication, object())
        self.samples.append(sample)
        return cast(UsageApplication, object())

    def list_usage_samples(self, run_id: str) -> list[UsageSample]:
        return [sample for sample in self.samples if sample.run_id == run_id]

    def enqueue_run_command(self, command: RunCommand) -> None:
        self.commands.append(command)

    def list_pending_run_commands(self, run_id: str | None = None) -> list[RunCommand]:
        return [
            command
            for command in self.commands
            if command.id not in self.acknowledgements
            and (run_id is None or command.run_id == run_id)
        ]

    def acknowledge_run_command(self, acknowledgement: RunCommandAck) -> None:
        self.acknowledgements[acknowledgement.command_id] = acknowledgement


@dataclass(frozen=True, slots=True)
class TurnCall:
    thread_id: str
    prompt: str
    cwd: str
    model: str
    effort: str
    output_schema: Mapping[str, JsonValue]


@dataclass(slots=True)
class ScriptedAppServerClient:
    turn_id: str
    scripted_events: tuple[AppServerEvent, ...]
    gate: asyncio.Event | None = None
    gate_at_index: int = 1
    started: bool = False
    closed: bool = False
    resumed: list[tuple[str, str, str]] = field(default_factory=list)
    turn_calls: list[TurnCall] = field(default_factory=list)
    steers: list[tuple[str, str, str]] = field(default_factory=list)
    interrupts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def metadata(self) -> AppServerMetadata:
        return AppServerMetadata(
            sdk_version=PINNED_OPENAI_CODEX_VERSION,
            runtime_version=PINNED_OPENAI_CODEX_VERSION,
            server_name="codex-app-server",
            platform_family="unix",
            platform_os="linux",
        )

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def start_thread(self, *, cwd: str, model: str) -> str:
        assert model == "gpt-6-luna"
        return "thread-1"

    async def resume_thread(self, thread_id: str, *, cwd: str, model: str) -> str:
        self.resumed.append((thread_id, cwd, model))
        return thread_id

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        model: str,
        effort: str,
        output_schema: Mapping[str, JsonValue],
    ) -> str:
        self.turn_calls.append(
            TurnCall(
                thread_id=thread_id,
                prompt=prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                output_schema=output_schema,
            )
        )
        return self.turn_id

    async def events(self, turn_id: str) -> AsyncIterator[AppServerEvent]:
        for index, event in enumerate(self.scripted_events):
            if index == self.gate_at_index and self.gate is not None:
                await self.gate.wait()
            yield event

    async def steer(self, thread_id: str, turn_id: str, instruction: str) -> None:
        self.steers.append((thread_id, turn_id, instruction))

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    async def account_rate_limits(self) -> dict[str, JsonValue]:
        raise AssertionError("run client must not query account rate limits")


@dataclass(frozen=True, slots=True)
class RawNotification:
    method: str
    payload: dict[str, JsonValue]


@dataclass(slots=True)
class ScriptedNotificationTransport:
    notifications: list[RawNotification]
    unregistered: list[str] = field(default_factory=list)

    async def next_turn_notification(self, turn_id: str) -> RawNotification:
        assert turn_id == "turn-1"
        return self.notifications.pop(0)

    def unregister_turn_notifications(self, turn_id: str) -> None:
        self.unregistered.append(turn_id)


def _usage_event(
    turn_id: str,
    *,
    last_input_tokens: int = 12,
    last_output_tokens: int = 8,
    total_input_tokens: int | None = None,
    total_cached_input_tokens: int | None = None,
    total_output_tokens: int | None = None,
    total_reasoning_output_tokens: int | None = None,
) -> AppServerEvent:
    last = {
        "inputTokens": last_input_tokens,
        "cachedInputTokens": min(3, last_input_tokens),
        "outputTokens": last_output_tokens,
        "reasoningOutputTokens": min(2, last_output_tokens),
        "totalTokens": last_input_tokens + last_output_tokens,
    }
    total_input = (
        last_input_tokens if total_input_tokens is None else total_input_tokens
    )
    total_output = (
        last_output_tokens if total_output_tokens is None else total_output_tokens
    )
    total = {
        "inputTokens": total_input,
        "cachedInputTokens": (
            min(3, total_input)
            if total_cached_input_tokens is None
            else total_cached_input_tokens
        ),
        "outputTokens": total_output,
        "reasoningOutputTokens": (
            min(2, total_output)
            if total_reasoning_output_tokens is None
            else total_reasoning_output_tokens
        ),
        "totalTokens": total_input + total_output,
    }
    return AppServerEvent(
        "thread/tokenUsage/updated",
        {
            "threadId": "thread-1",
            "turnId": turn_id,
            "tokenUsage": {"last": last, "total": total},
        },
    )


def _structured_response() -> str:
    return json.dumps(
        {
            "summary": "implementation ready for review",
            "completed": ["implementation", "tests"],
            "current": [],
            "next_steps": ["review"],
            "known_failures": [],
            "decisions": ["kept lifecycle outside Codex"],
            "commit": "abc123",
            "review_requested": True,
        }
    )


def _completed_events(
    turn_id: str,
    *,
    total_input_tokens: int = 12,
    total_cached_input_tokens: int = 3,
    total_output_tokens: int = 8,
    total_reasoning_output_tokens: int = 2,
) -> tuple[AppServerEvent, ...]:
    return (
        AppServerEvent("turn/started", {"turn": {"id": turn_id}}),
        _usage_event(
            turn_id,
            total_input_tokens=total_input_tokens,
            total_cached_input_tokens=total_cached_input_tokens,
            total_output_tokens=total_output_tokens,
            total_reasoning_output_tokens=total_reasoning_output_tokens,
        ),
        AppServerEvent(
            "item/completed",
            {
                "turnId": turn_id,
                "item": {
                    "id": "item-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": _structured_response(),
                },
            },
        ),
        AppServerEvent(
            "turn/completed",
            {
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "error": None,
                }
            },
        ),
    )


def test_sdk_driver_streams_usage_commands_and_structured_terminal_result(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        gate = asyncio.Event()
        completed = _completed_events("turn-1")
        client = ScriptedAppServerClient(
            "turn-1",
            (
                completed[0],
                completed[1],
                _usage_event(
                    "turn-1",
                    last_input_tokens=2,
                    last_output_tokens=1,
                    total_input_tokens=14,
                    total_cached_input_tokens=5,
                    total_output_tokens=9,
                    total_reasoning_output_tokens=3,
                ),
                *completed[2:],
            ),
            gate=gate,
        )
        store = MemorySupervisorStore()
        supervisor = RunSupervisor(
            store,
            client_factory=lambda _execution: client,
        )
        driver = CodexSdkDriver(supervisor)

        handle = await driver.start_managed("run-1", execution)
        await asyncio.sleep(0)
        initial = driver.observe("run-1")
        assert initial is not None and not initial.terminal

        await driver.steer(handle, "run focused tests")
        checkpoint = RunCommand(
            id="policy-checkpoint-1",
            run_id="run-1",
            action="checkpoint",
        )
        store.enqueue_run_command(checkpoint)
        await driver.process_pending("run-1")
        suspend = RunCommand(
            id="policy-suspend-1",
            run_id="run-1",
            action="suspend",
        )
        store.enqueue_run_command(suspend)
        await driver.process_pending("run-1")
        await driver.process_pending("run-1")
        await driver.interrupt(handle)
        gate.set()
        result = await driver.collect(handle)
        second = await driver.collect(handle)

        assert result is second
        assert result.outcome is RunOutcome.COMPLETED
        assert result.summary == "implementation ready for review"
        assert result.commit == "abc123"
        assert result.consumed_quota == 0
        assert result.usage is not None
        assert result.usage.total_tokens == 23
        assert result.metadata["review_requested"] is True
        assert result.metadata["telemetry_valid"] is True

        terminal = driver.observe("run-1")
        assert terminal is not None and terminal.terminal
        assert terminal.telemetry_valid
        assert terminal.result == result
        assert terminal.run_state is RunState.COMPLETED
        assert terminal.unit is QuotaUnit.TOKENS
        assert not store.sessions["run-1"].active
        assert len(store.samples) == 3
        assert store.samples[0].tokens is not None
        assert store.samples[0].tokens.total_tokens == 20
        assert not store.samples[0].final
        assert store.samples[1].tokens == terminal.usage
        assert not store.samples[1].final
        assert store.samples[2].tokens == terminal.usage
        assert store.samples[2].final
        assert set(store.acknowledgements) == {
            store.commands[0].id,
            "policy-checkpoint-1",
            "policy-suspend-1",
            store.commands[-1].id,
        }
        assert "run focused tests" in client.steers[0][2]
        assert "structured durable checkpoint" in client.steers[1][2]
        assert "do not begin more work" in client.steers[2][2]
        assert len(client.steers) == 3
        assert client.interrupts == [("thread-1", "turn-1")]

        call = client.turn_calls[0]
        assert call.model == "gpt-6-luna"
        assert call.effort == "xhigh"
        assert call.output_schema["additionalProperties"] is False
        assert client.closed

    asyncio.run(scenario())


def test_supervisor_recovers_thread_and_replays_pending_checkpoint(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        blocked = asyncio.Event()
        first = ScriptedAppServerClient(
            "turn-1",
            (
                AppServerEvent("turn/started", {"turn": {"id": "turn-1"}}),
                _usage_event("turn-1"),
                AppServerEvent("item/started", {"turnId": "turn-1"}),
            ),
            gate=blocked,
            gate_at_index=2,
        )
        store = MemorySupervisorStore()
        first_supervisor = RunSupervisor(
            store,
            client_factory=lambda _execution: first,
        )
        first_driver = CodexSdkDriver(first_supervisor)
        await first_driver.start_managed("run-1", execution)
        await asyncio.sleep(0)
        await first_driver.close()

        assert store.sessions["run-1"].active
        store.enqueue_run_command(
            RunCommand(
                id="checkpoint-after-restart",
                run_id="run-1",
                action="checkpoint",
            )
        )

        second = ScriptedAppServerClient(
            "turn-2",
            _completed_events(
                "turn-2",
                total_input_tokens=24,
                total_cached_input_tokens=6,
                total_output_tokens=16,
                total_reasoning_output_tokens=4,
            ),
        )
        second_supervisor = RunSupervisor(
            store,
            client_factory=lambda _execution: second,
        )
        second_driver = CodexSdkDriver(second_supervisor)
        handle = await second_driver.recover("run-1", execution)
        result = await second_driver.collect(handle)

        assert second.resumed == [("thread-1", "/workspace", "gpt-6-luna")]
        assert "Current bounded assignment" in second.turn_calls[0].prompt
        assert "structured durable checkpoint" in second.steers[0][2]
        assert "checkpoint-after-restart" in store.acknowledgements
        assert result.outcome is RunOutcome.COMPLETED
        assert result.usage is not None
        assert result.usage.total_tokens == 40
        assert store.sessions["run-1"].metadata["recovery_count"] == 1

    asyncio.run(scenario())


def test_supervisor_continues_terminal_thread_without_double_charging(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        first = ScriptedAppServerClient(
            "turn-1",
            _completed_events("turn-1"),
        )
        second = ScriptedAppServerClient(
            "turn-2",
            _completed_events(
                "turn-2",
                total_input_tokens=24,
                total_cached_input_tokens=6,
                total_output_tokens=16,
                total_reasoning_output_tokens=4,
            ),
        )
        clients = iter((first, second))
        store = MemorySupervisorStore(runs={"run-1": StoredRun(contract=execution)})
        driver = CodexSdkDriver(
            RunSupervisor(
                store,
                client_factory=lambda _execution: next(clients),
            )
        )

        first_handle = await driver.start_managed("run-1", execution)
        first_result = await driver.collect(first_handle)
        assert first_result.usage is not None
        assert first_result.usage.total_tokens == 20
        assert not store.sessions["run-1"].active

        second_handle = await driver.continue_turn(
            "run-1",
            "Repair the review findings and request review again.",
        )
        second_result = await driver.collect(second_handle)

        assert second_handle.id == first_handle.id
        assert second.resumed == [("thread-1", "/workspace", "gpt-6-luna")]
        assert "Repair the review findings" in second.turn_calls[0].prompt
        assert second_result.usage is not None
        assert second_result.usage.total_tokens == 40
        assert [
            sample.cumulative_quota for sample in store.samples if sample.final
        ] == [20, 20]
        assert store.sessions["run-1"].metadata["continuation_count"] == 1

    asyncio.run(scenario())


def test_failed_terminal_without_usage_is_observable_but_telemetry_invalid(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        client = ScriptedAppServerClient(
            "turn-1",
            (
                AppServerEvent(
                    "turn/failed",
                    {"message": "provider rejected the turn"},
                ),
            ),
        )
        store = MemorySupervisorStore()
        driver = CodexSdkDriver(
            RunSupervisor(store, client_factory=lambda _execution: client)
        )
        handle = await driver.start_managed("run-1", execution)
        result = await driver.collect(handle)
        observation = driver.observe("run-1")

        assert result.outcome is RunOutcome.FAILED
        assert result.summary == "provider rejected the turn"
        assert observation is not None and observation.terminal
        assert not observation.telemetry_valid
        assert observation.result == result
        assert observation.run_state is RunState.FAILED
        assert store.samples == []

    asyncio.run(scenario())


def test_retryable_error_does_not_end_the_managed_turn(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        completed = _completed_events("turn-1")
        client = ScriptedAppServerClient(
            "turn-1",
            (
                completed[0],
                AppServerEvent(
                    "error",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "willRetry": True,
                        "error": {"message": "temporary provider overload"},
                    },
                ),
                *completed[1:],
            ),
        )
        store = MemorySupervisorStore()
        driver = CodexSdkDriver(
            RunSupervisor(store, client_factory=lambda _execution: client)
        )

        handle = await driver.start_managed("run-1", execution)
        result = await driver.collect(handle)
        observation = driver.observe("run-1")

        assert result.outcome is RunOutcome.COMPLETED
        assert result.summary == "implementation ready for review"
        assert observation is not None and observation.terminal
        assert observation.run_state is RunState.COMPLETED
        assert store.sessions["run-1"].observation_cursor == "6"
        assert client.closed

    asyncio.run(scenario())


def test_app_server_adapter_streams_past_retryable_error() -> None:
    async def scenario() -> None:
        transport = ScriptedNotificationTransport(
            [
                RawNotification(
                    "error",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "willRetry": True,
                        "error": {"message": "temporary provider overload"},
                    },
                ),
                RawNotification(
                    "turn/completed",
                    {
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "error": None,
                        }
                    },
                ),
            ]
        )
        client = object.__new__(OpenAICodexClient)
        client._client = transport

        events = [event async for event in client.events("turn-1")]

        assert [event.method for event in events] == ["error", "turn/completed"]
        assert events[0].payload["willRetry"] is True
        assert transport.unregistered == ["turn-1"]

    asyncio.run(scenario())


def test_stream_exhaustion_becomes_an_observable_terminal_failure(
    execution_contract: ExecutionContract,
) -> None:
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    async def scenario() -> None:
        client = ScriptedAppServerClient(
            "turn-1",
            (
                AppServerEvent("turn/started", {"turn": {"id": "turn-1"}}),
                _usage_event("turn-1"),
            ),
        )
        store = MemorySupervisorStore()
        driver = CodexSdkDriver(
            RunSupervisor(store, client_factory=lambda _execution: client)
        )

        handle = await driver.start_managed("run-1", execution)
        result = await driver.collect(handle)
        observation = driver.observe("run-1")

        assert result.outcome is RunOutcome.FAILED
        assert "without a terminal event" in result.summary
        assert observation is not None and observation.terminal
        assert not observation.telemetry_valid
        assert observation.result == result
        assert len(store.samples) == 1
        assert not store.samples[0].final

    asyncio.run(scenario())


def test_sdk_driver_rejects_write_scope_outside_the_leased_worktree(
    execution_contract: ExecutionContract,
) -> None:
    client = ScriptedAppServerClient("turn-1", _completed_events("turn-1"))
    driver = CodexSdkDriver(
        RunSupervisor(
            MemorySupervisorStore(),
            client_factory=lambda _execution: client,
        )
    )

    async def scenario() -> None:
        try:
            await driver.start_managed("run-1", execution_contract)
        except ValueError as error:
            assert "leased worktree" in str(error)
        else:
            raise AssertionError("out-of-worktree write scope was accepted")

    asyncio.run(scenario())
    assert not client.started


def test_supervisor_rejects_workspace_or_read_root_overlapping_codex_state(
    execution_contract: ExecutionContract,
) -> None:
    codex_state = Path.home() / ".codex"
    client = ScriptedAppServerClient("turn-1", _completed_events("turn-1"))
    store = MemorySupervisorStore()
    driver = CodexSdkDriver(
        RunSupervisor(store, client_factory=lambda _execution: client)
    )
    exposed = replace(
        execution_contract,
        working_directory=str(codex_state / "workspace"),
        allowed_filesystem_scope=(str(codex_state / "workspace"),),
    )

    async def scenario() -> None:
        try:
            await driver.start_managed("run-1", exposed)
        except ValueError as error:
            assert "account state" in str(error)
        else:
            raise AssertionError("Codex account state was exposed as a workspace")

    asyncio.run(scenario())
    assert not client.started


@pytest.mark.parametrize(
    "saved_final,terminal_status",
    [
        (False, "completed"),
        (True, "completed"),
        (True, "failed"),
        (True, "interrupted"),
    ],
)
def test_readonly_recovery_preserves_turn_and_charges_native_final_usage_once(
    execution_contract, saved_final, terminal_status
):
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )
    reads = []

    class ReadOnlyClient(ScriptedAppServerClient):
        async def read_thread(self, thread_id):
            reads.append(thread_id)
            return {
                "id": thread_id,
                "cwd": execution.working_directory,
                "cliVersion": PINNED_OPENAI_CODEX_VERSION,
                "status": {"type": "notLoaded"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": terminal_status,
                        "items": [
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": _structured_response(),
                            }
                        ],
                    }
                ],
            }

    client = ReadOnlyClient("turn-1", ())
    partial = TokenUsage(input_tokens=5, output_tokens=3)
    final = TokenUsage(input_tokens=12, output_tokens=8)
    store = MemorySupervisorStore(
        sessions={
            "run-1": DriverSession(
                "run-1",
                "codex",
                thread_id="thread-1",
                turn_id="turn-1",
                observation_cursor="2",
                metadata={
                    "sdk_version": PINNED_OPENAI_CODEX_VERSION,
                    "runtime_version": PINNED_OPENAI_CODEX_VERSION,
                },
            )
        },
        runs={"run-1": StoredRun(execution)},
        samples=[
            UsageSample(
                "run-1", "thread-1", "turn-1", 1, 8, QuotaUnit.TOKENS, tokens=partial
            )
        ],
    )
    if saved_final:
        store.samples.append(
            UsageSample(
                "run-1",
                "thread-1",
                "turn-1",
                3,
                20,
                QuotaUnit.TOKENS,
                tokens=final,
                final=True,
            )
        )
    supervisor = RunSupervisor(store, client_factory=lambda _execution: client)

    async def scenario():
        def proof(*_args):
            if saved_final:
                raise AssertionError(
                    "Persisted final SDK usage must not require native proof"
                )
            return NativeTerminalUsage(final, "a" * 64)

        result = await supervisor.recover_terminal_readonly(
            "run-1", terminal_usage_reader=proof
        )
        assert result is not None and result.terminal and result.telemetry_valid
        assert (
            result.result.outcome
            is {
                "completed": RunOutcome.COMPLETED,
                "failed": RunOutcome.FAILED,
                "interrupted": RunOutcome.CANCELLED,
            }[terminal_status]
        )
        assert result.result.usage == final
        assert result.result.summary == "implementation ready for review"
        assert result.thread_id == "thread-1" and result.turn_id == "turn-1"
        assert await supervisor.recover_terminal_readonly("run-1") == result

    asyncio.run(scenario())
    assert reads == ["thread-1"]
    assert client.closed and client.started
    assert not client.turn_calls and not client.resumed and not client.steers
    assert len(store.samples) == 2
    assert store.samples[-1].final
    assert not store.sessions["run-1"].active


@pytest.mark.parametrize(
    "fault", ["unknown-usage", "unknown-turn", "other-turn", "active", "backwards"]
)
def test_readonly_recovery_blocks_unknown_ownership_and_usage(
    execution_contract, fault
):
    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
    )

    class ReadOnlyClient(ScriptedAppServerClient):
        async def read_thread(self, thread_id):
            return {
                "id": thread_id,
                "cwd": execution.working_directory,
                "cliVersion": PINNED_OPENAI_CODEX_VERSION,
                "status": {"type": "active" if fault == "active" else "notLoaded"},
                "turns": [
                    {
                        "id": "other" if fault == "other-turn" else "turn-1",
                        "status": "completed",
                        "items": [],
                    }
                ],
            }

    client = ReadOnlyClient("turn-1", ())
    store = MemorySupervisorStore(
        sessions={
            "run-1": DriverSession(
                "run-1",
                "codex",
                thread_id="thread-1",
                turn_id=None if fault == "unknown-turn" else "turn-1",
                observation_cursor="2",
                metadata={
                    "sdk_version": PINNED_OPENAI_CODEX_VERSION,
                    "runtime_version": PINNED_OPENAI_CODEX_VERSION,
                },
            )
        },
        runs={"run-1": StoredRun(execution)},
        samples=[
            UsageSample(
                "run-1",
                "thread-1",
                "turn-1",
                1,
                8,
                QuotaUnit.TOKENS,
                tokens=TokenUsage(input_tokens=5, output_tokens=3),
            )
        ],
    )
    supervisor = RunSupervisor(store, client_factory=lambda _execution: client)

    def proof(*_args):
        if fault == "backwards":
            raise ValueError("native counters moved backwards")
        return NativeTerminalUsage(
            TokenUsage(input_tokens=12, output_tokens=8), "a" * 64
        )

    async def scenario():
        if fault in {"unknown-usage", "unknown-turn"}:
            assert await supervisor.recover_terminal_readonly("run-1") is None
        else:
            with pytest.raises(ValueError):
                await supervisor.recover_terminal_readonly(
                    "run-1", terminal_usage_reader=proof
                )

    asyncio.run(scenario())
    assert len(store.samples) == 1
    assert store.sessions["run-1"].active
    assert not client.turn_calls and not client.resumed


def test_sdk_uncached_usage_keeps_provider_counters(execution_contract):
    from agentd.coding.models import CodingWorkOrder
    from agentd.domain.models import CodingOperation

    execution = replace(
        execution_contract,
        allowed_filesystem_scope=(execution_contract.working_directory,),
        operation=CodingOperation(
            CodingWorkOrder(
                job_id=execution_contract.job_id,
                repository="test/repo",
                profile_id="test",
                profile_version="1",
                profile_digest="a" * 64,
                base_commit="b" * 40,
                source_revision="c" * 64,
                objective="test",
                harness="codex",
                account_pool_id="account",
                expected_quota=100,
                maximum_quota=900,
                max_runtime_seconds=60,
                quota_basis="uncached-v1",
                maximum_run_quota=300,
            )
        ),
    )

    async def scenario():
        client = ScriptedAppServerClient("turn-1", _completed_events("turn-1"))
        store = MemorySupervisorStore()
        supervisor = RunSupervisor(store, client_factory=lambda _: client)
        driver = CodexSdkDriver(supervisor)
        handle = await driver.start_managed("uncached-sdk", execution)
        result = await driver.collect(handle)
        assert result.metadata["telemetry_valid"] is True
        assert result.usage.cached_input_tokens > 0
        for sample in store.samples:
            assert sample.metadata["quota_basis"] == "uncached-v1"
            assert sample.cumulative_quota == sample.tokens.uncached_tokens
            assert sample.tokens.total_tokens > sample.cumulative_quota
        observation = driver.observe(handle.id)
        assert observation.normalized_cumulative_quota == result.usage.uncached_tokens
        await supervisor.close()

    asyncio.run(scenario())
