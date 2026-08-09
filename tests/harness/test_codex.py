import asyncio
import json
import os
import signal
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

from agentd.domain.enums import RunOutcome
from agentd.domain.models import ExecutionContract, JsonValue
from agentd.harness.codex import CodexDriver, render_execution_contract


@dataclass(slots=True)
class StubProcess:
    stdout: bytes
    stderr: bytes = b""
    final_returncode: int = 0
    pid: int | None = 123
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    terminated: bool = False
    killed: bool = False
    communicate_count: int = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_count += 1
        self.returncode = self.final_returncode
        return self.stdout, self.stderr

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@dataclass(slots=True)
class InterruptHangingProcess:
    pid: int | None = 456
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    terminated: bool = False
    killed: bool = False
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def communicate(self) -> tuple[bytes, bytes]:
        await self.stopped.wait()
        return b"", b""

    async def wait(self) -> int:
        await self.stopped.wait()
        return self.returncode or 0

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -signal.SIGTERM
        self.stopped.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL
        self.stopped.set()


@dataclass(frozen=True, slots=True)
class ProcessCall:
    command: tuple[str, ...]
    working_directory: str
    environment: Mapping[str, str]


class RecordingProcessFactory:
    def __init__(self, *processes: StubProcess) -> None:
        self._processes = iter(processes)
        self.calls: list[ProcessCall] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        working_directory: str,
        environment: Mapping[str, str],
    ) -> StubProcess:
        self.calls.append(ProcessCall(command, working_directory, dict(environment)))
        return next(self._processes)


def event(**values: JsonValue) -> bytes:
    return (json.dumps(values) + "\n").encode()


def test_codex_command_is_shell_free_and_contract_focused(
    execution_contract: ExecutionContract,
) -> None:
    execution_contract = replace(
        execution_contract,
        objective="Fix it; touch /tmp/not-run && $(also-not-run)",
    )
    driver = CodexDriver(
        executable="/opt/codex binary",
        model_aliases={"standard": "gpt-test"},
        base_environment={},
    )

    command = driver.build_command(execution_contract)
    prompt = command[-1]

    assert command[:-1] == (
        "/opt/codex binary",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        "/workspace",
        "--add-dir",
        "/shared",
        "--model",
        "gpt-test",
        "exec",
        "--json",
        "--color",
        "never",
    )
    assert "Fix it; touch /tmp/not-run && $(also-not-run)" in prompt
    assert "- dep-a: first\n- dep-b: second" in prompt
    assert "- completed: schema" in prompt
    assert "TASK_TOKEN" not in prompt
    assert "secret-value" not in prompt


def test_codex_driver_starts_and_collects_jsonl_result(
    execution_contract: ExecutionContract,
    deterministic_ids: Iterator[str],
) -> None:
    process = StubProcess(
        stdout=b"".join(
            (
                event(type="thread.started", thread_id="session-1"),
                event(
                    type="item.completed",
                    item={"type": "agent_message", "text": "done"},
                ),
                event(type="turn.completed", usage={"input_tokens": 7}),
            )
        )
    )
    factory = RecordingProcessFactory(process)
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: next(deterministic_ids),
        base_environment={"BASE": "present"},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        result = await driver.collect(run)
        second_result = await driver.collect(run)

        assert run.id == "run-1"
        assert run.external_id == "123"
        assert result is second_result
        assert result.outcome is RunOutcome.COMPLETED
        assert result.summary == "done"
        assert result.metadata["session_id"] == "session-1"
        assert result.metadata["event_count"] == 3
        assert result.metadata["usage"] == {"input_tokens": 7}
        assert process.communicate_count == 1
        assert factory.calls[0].working_directory == "/workspace"
        assert factory.calls[0].environment == {
            "BASE": "present",
            "TASK_TOKEN": "secret-value",
        }

    asyncio.run(scenario())


def test_codex_driver_delivers_steering_at_turn_boundary(
    execution_contract: ExecutionContract,
) -> None:
    first = StubProcess(
        stdout=b"".join(
            (
                event(type="thread.started", thread_id="session-1"),
                event(
                    type="item.completed",
                    item={"type": "agent_message", "text": "first"},
                ),
                event(type="turn.completed", usage={"output_tokens": 2}),
            )
        )
    )
    second = StubProcess(
        stdout=b"".join(
            (
                event(type="thread.started", thread_id="session-1"),
                event(
                    type="item.completed",
                    item={"type": "agent_message", "text": "revised"},
                ),
                event(type="turn.completed", usage={"output_tokens": 3}),
            )
        ),
        pid=124,
    )
    factory = RecordingProcessFactory(first, second)
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.steer(run, "Also run the integration test")
        result = await driver.collect(run)

        assert result.outcome is RunOutcome.COMPLETED
        assert result.summary == "revised"
        assert result.metadata["steering_turns"] == 1
        assert result.metadata["usage"] == {"output_tokens": 5}
        assert len(factory.calls) == 2
        resume_command = factory.calls[1].command
        assert resume_command[-5:] == (
            "exec",
            "resume",
            "--json",
            "session-1",
            "Also run the integration test",
        )

    asyncio.run(scenario())


def test_codex_driver_fails_if_steering_has_no_session_id(
    execution_contract: ExecutionContract,
) -> None:
    factory = RecordingProcessFactory(StubProcess(stdout=event(type="turn.completed")))
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.steer(run, "continue")
        result = await driver.collect(run)

        assert result.outcome is RunOutcome.FAILED
        assert "session ID" in result.summary
        assert len(factory.calls) == 1

    asyncio.run(scenario())


def test_codex_driver_reports_process_failure(
    execution_contract: ExecutionContract,
) -> None:
    factory = RecordingProcessFactory(
        StubProcess(stdout=b"", stderr=b"authentication failed", final_returncode=4)
    )
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        result = await driver.collect(run)

        assert result.outcome is RunOutcome.FAILED
        assert result.summary == "authentication failed"
        assert result.metadata["returncode"] == 4

    asyncio.run(scenario())


def test_codex_driver_interrupts_without_starting_a_steering_turn(
    execution_contract: ExecutionContract,
) -> None:
    process = StubProcess(stdout=b"", final_returncode=-2)
    factory = RecordingProcessFactory(process)
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.steer(run, "queued")
        await driver.interrupt(run)
        result = await driver.collect(run)

        assert process.signals == [signal.SIGINT]
        assert result.outcome is RunOutcome.CANCELLED
        assert result.metadata["interrupted"] is True
        assert len(factory.calls) == 1

    asyncio.run(scenario())


def test_codex_interrupt_escalates_when_process_does_not_exit(
    execution_contract: ExecutionContract,
) -> None:
    process = InterruptHangingProcess()
    factory = RecordingProcessFactory(process)  # type: ignore[arg-type]
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
        termination_timeout=0.01,
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.interrupt(run)
        result = await driver.collect(run)

        assert process.signals == [signal.SIGINT]
        assert process.terminated
        assert not process.killed
        assert result.outcome is RunOutcome.CANCELLED
        assert result.metadata["interrupt_escalated"] is True

    asyncio.run(scenario())


def test_codex_cancel_signals_the_owned_posix_process_group(
    execution_contract: ExecutionContract,
    monkeypatch,
) -> None:
    if os.name != "posix":
        return
    process = StubProcess(stdout=b"", final_returncode=-signal.SIGTERM)
    factory = RecordingProcessFactory(process)
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sig: group_signals.append((process_group, sig)),
    )
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
        process_group_signals=True,
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.cancel(run)

        assert group_signals == [
            (123, signal.SIGTERM),
            (123, signal.SIGKILL),
        ]
        assert not process.terminated
        assert not process.killed

    asyncio.run(scenario())


def test_codex_driver_cancels_process(
    execution_contract: ExecutionContract,
) -> None:
    process = StubProcess(stdout=b"", final_returncode=-15)
    factory = RecordingProcessFactory(process)
    driver = CodexDriver(
        process_factory=factory,
        id_factory=lambda: "run-1",
        base_environment={},
    )

    async def scenario() -> None:
        run = await driver.start(execution_contract)
        await driver.cancel(run)
        result = await driver.collect(run)

        assert process.terminated
        assert not process.killed
        assert result.outcome is RunOutcome.CANCELLED
        assert result.metadata["cancelled"] is True

    asyncio.run(scenario())


def test_render_execution_contract_is_deterministic(
    execution_contract: ExecutionContract,
) -> None:
    assert render_execution_contract(execution_contract) == render_execution_contract(
        execution_contract
    )
