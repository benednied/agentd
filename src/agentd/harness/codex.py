"""Subprocess adapter for non-interactive Codex CLI runs.

The adapter translates an :class:`ExecutionContract` into Codex operations. It
does not make admission, quota, priority, or node-selection decisions.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from agentd.domain.enums import RunOutcome
from agentd.domain.models import (
    ExecutionContract,
    HarnessCapabilities,
    JsonValue,
    RunHandle,
    RunResult,
    new_id,
)
from agentd.harness.errors import RunNotActiveError, UnknownRunError


class ManagedProcess(Protocol):
    """The small subprocess surface used by :class:`CodexDriver`."""

    pid: int | None
    returncode: int | None

    async def communicate(self) -> tuple[bytes, bytes]: ...

    async def wait(self) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[
    [tuple[str, ...], str, Mapping[str, str]], Awaitable[ManagedProcess]
]


async def _create_process(
    command: tuple[str, ...],
    working_directory: str,
    environment: Mapping[str, str],
) -> ManagedProcess:
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=working_directory,
        env=dict(environment),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # A dedicated POSIX session lets cancellation terminate descendants as
        # well as the Codex CLI leader. Windows process-tree ownership remains a
        # future backend concern.
        start_new_session=os.name == "posix",
    )


@dataclass(slots=True)
class _ParsedOutput:
    summary: str = ""
    session_id: str | None = None
    event_count: int = 0
    malformed_line_count: int = 0
    usage: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class _CodexRun:
    execution: ExecutionContract
    process: ManagedProcess
    environment: dict[str, str]
    queued_instructions: list[str] = field(default_factory=list)
    session_id: str | None = None
    interrupted: bool = False
    cancelled: bool = False
    collected_result: RunResult | None = None
    interrupt_escalated: bool = False
    collect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CodexDriver:
    """Run execution contracts through ``codex exec`` without a shell."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        process_factory: ProcessFactory = _create_process,
        id_factory: Callable[[], str] = new_id,
        base_environment: Mapping[str, str] | None = None,
        model_aliases: Mapping[str, str] | None = None,
        models: frozenset[str] = frozenset({"standard"}),
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        termination_timeout: float = 5.0,
        process_group_signals: bool | None = None,
    ) -> None:
        if not executable.strip():
            raise ValueError("The Codex executable cannot be empty")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError(f"Unsupported Codex sandbox mode: {sandbox!r}")
        if approval_policy not in {"untrusted", "on-request", "never"}:
            raise ValueError(f"Unsupported Codex approval policy: {approval_policy!r}")
        if termination_timeout <= 0:
            raise ValueError("The process termination timeout must be positive")

        self._executable = executable
        self._process_factory = process_factory
        self._id_factory = id_factory
        self._base_environment = dict(
            os.environ if base_environment is None else base_environment
        )
        # "standard" is an abstract scheduler class, not a Codex model name.
        # With no explicit mapping, let the user's Codex configuration choose.
        self._model_aliases = {"standard": "", **dict(model_aliases or {})}
        self._sandbox = sandbox
        self._approval_policy = approval_policy
        self._termination_timeout = termination_timeout
        self._process_group_signals = (
            process_factory is _create_process and os.name == "posix"
            if process_group_signals is None
            else process_group_signals
        )
        if self._process_group_signals and os.name != "posix":
            raise ValueError("Process-group signalling requires a POSIX platform")
        self._capabilities = HarnessCapabilities(
            name="codex",
            models=models,
            features=frozenset(
                {
                    "checkpointing",
                    "subprocess",
                    "turn-boundary-steering",
                }
            ),
            native_pause=False,
            steering=True,
            checkpointing=True,
        )
        self._runs: dict[str, _CodexRun] = {}

    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    def build_command(self, execution: ExecutionContract) -> tuple[str, ...]:
        """Build a shell-free initial command suitable for inspection and testing."""

        return (
            *self._base_command(execution),
            "exec",
            "--json",
            "--color",
            "never",
            render_execution_contract(execution),
        )

    def build_resume_command(
        self,
        execution: ExecutionContract,
        session_id: str,
        instruction: str,
    ) -> tuple[str, ...]:
        """Build a turn-boundary steering command for a persisted exec session."""

        if not session_id.strip():
            raise ValueError("A Codex session ID is required for steering")
        if not instruction.strip():
            raise ValueError("A steering instruction cannot be empty")
        return (
            *self._base_command(execution),
            "exec",
            "resume",
            "--json",
            session_id,
            instruction,
        )

    async def start(self, execution: ExecutionContract) -> RunHandle:
        command = self.build_command(execution)
        environment = {**self._base_environment, **execution.environment}
        process = await self._process_factory(
            command,
            execution.working_directory,
            environment,
        )
        run_id = self._id_factory()
        handle = RunHandle(
            id=run_id,
            driver=self._capabilities.name,
            external_id=str(process.pid) if process.pid is not None else None,
        )
        self._runs[run_id] = _CodexRun(
            execution=execution,
            process=process,
            environment=environment,
        )
        return handle

    async def steer(self, run: RunHandle, instruction: str) -> None:
        if not instruction.strip():
            raise ValueError("A steering instruction cannot be empty")
        state = self._active_state(run)
        state.queued_instructions.append(instruction)

    async def interrupt(self, run: RunHandle) -> None:
        state = self._state(run)
        if state.collected_result is not None or state.interrupted:
            return
        state.interrupted = True
        state.queued_instructions.clear()
        if state.process.returncode is None:
            self._signal_process(state, signal.SIGINT)

    async def collect(self, run: RunHandle) -> RunResult:
        state = self._state(run)
        async with state.collect_lock:
            if state.collected_result is not None:
                return state.collected_result
            state.collected_result = await self._collect(state)
            return state.collected_result

    async def cancel(self, run: RunHandle) -> None:
        state = self._state(run)
        if state.collected_result is not None or state.cancelled:
            return
        state.cancelled = True
        state.queued_instructions.clear()
        if state.process.returncode is not None:
            return
        self._signal_process(state, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                state.process.wait(), timeout=self._termination_timeout
            )
        except TimeoutError:
            self._signal_process(state, signal.SIGKILL)
            await state.process.wait()
        if self._process_group_signals:
            # The leader may exit before a stubborn descendant. The group is
            # exclusively owned by this run, so force-close any survivors.
            self._signal_process(state, signal.SIGKILL)

    def _base_command(self, execution: ExecutionContract) -> tuple[str, ...]:
        command = [
            self._executable,
            "--ask-for-approval",
            self._approval_policy,
            "--sandbox",
            self._sandbox,
            "--cd",
            execution.working_directory,
        ]
        working_directory = os.path.normpath(execution.working_directory)
        seen_scopes = {working_directory}
        for scope in execution.allowed_filesystem_scope:
            normalized = os.path.normpath(scope)
            if normalized in seen_scopes:
                continue
            command.extend(("--add-dir", scope))
            seen_scopes.add(normalized)
        model = self._model_aliases.get(
            execution.model_class, execution.model_class
        ).strip()
        if model:
            command.extend(("--model", model))
        return tuple(command)

    async def _collect(self, state: _CodexRun) -> RunResult:
        event_count = 0
        malformed_line_count = 0
        steering_turns = 0
        summaries: list[str] = []
        stderr_chunks: list[str] = []
        usage: dict[str, JsonValue] = {}
        returncode: int | None = None
        collection_error: str | None = None

        while True:
            try:
                stdout, stderr = await self._communicate(state)
            except Exception as error:  # pragma: no cover - process integration guard
                collection_error = f"Could not collect Codex process: {error}"
                break

            if state.process.returncode is None:
                returncode = await state.process.wait()
            else:
                returncode = state.process.returncode

            parsed = _parse_output(stdout.decode(errors="replace"))
            event_count += parsed.event_count
            malformed_line_count += parsed.malformed_line_count
            if parsed.summary:
                summaries.append(parsed.summary)
            if parsed.session_id:
                state.session_id = parsed.session_id
            _merge_usage(usage, parsed.usage)

            decoded_stderr = stderr.decode(errors="replace").strip()
            if decoded_stderr:
                stderr_chunks.append(decoded_stderr)

            if state.cancelled or state.interrupted or returncode != 0:
                break
            if not state.queued_instructions:
                break
            if state.session_id is None:
                collection_error = (
                    "Codex did not report a session ID; queued steering could not "
                    "be delivered"
                )
                break

            instruction = state.queued_instructions.pop(0)
            command = self.build_resume_command(
                state.execution,
                state.session_id,
                instruction,
            )
            if self._process_group_signals:
                # Each resumed CLI invocation receives its own session. Retire
                # descendants of the completed turn before replacing its handle.
                self._signal_process(state, signal.SIGKILL)
            try:
                state.process = await self._process_factory(
                    command,
                    state.execution.working_directory,
                    state.environment,
                )
            except Exception as error:  # pragma: no cover - process integration guard
                collection_error = f"Could not resume Codex session: {error}"
                break
            steering_turns += 1

        if self._process_group_signals:
            # A completed CLI leader does not prove that agent-spawned children
            # exited. The session is exclusively owned, so no descendant may
            # survive beyond result collection and workspace release.
            self._signal_process(state, signal.SIGKILL)

        if state.cancelled:
            outcome = RunOutcome.CANCELLED
            fallback_summary = "Codex run cancelled"
        elif state.interrupted:
            outcome = RunOutcome.CANCELLED
            fallback_summary = "Codex run interrupted"
        elif collection_error is not None or returncode != 0:
            outcome = RunOutcome.FAILED
            fallback_summary = collection_error or "Codex run failed"
        else:
            outcome = RunOutcome.COMPLETED
            fallback_summary = "Codex run completed"

        summary = summaries[-1] if summaries else ""
        if outcome is RunOutcome.FAILED and stderr_chunks:
            summary = stderr_chunks[-1]
        if not summary:
            summary = fallback_summary

        metadata: dict[str, JsonValue] = {
            "returncode": returncode,
            "session_id": state.session_id,
            "event_count": event_count,
            "malformed_line_count": malformed_line_count,
            "stderr": "\n".join(stderr_chunks),
            "interrupted": state.interrupted,
            "cancelled": state.cancelled,
            "steering_turns": steering_turns,
            "interrupt_escalated": state.interrupt_escalated,
        }
        if collection_error is not None:
            metadata["collection_error"] = collection_error
        if usage:
            metadata["usage"] = usage

        return RunResult(
            outcome=outcome,
            summary=summary,
            metadata=metadata,
        )

    async def _communicate(self, state: _CodexRun) -> tuple[bytes, bytes]:
        """Drain output, bounding shutdown after interrupt/cancellation."""

        if not state.interrupted and not state.cancelled:
            return await state.process.communicate()

        communication = asyncio.create_task(state.process.communicate())
        try:
            return await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self._termination_timeout,
            )
        except TimeoutError:
            state.interrupt_escalated = True
            self._signal_process(state, signal.SIGTERM)

        try:
            return await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self._termination_timeout,
            )
        except TimeoutError:
            self._signal_process(state, signal.SIGKILL)

        try:
            return await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self._termination_timeout,
            )
        except TimeoutError as error:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise RuntimeError(
                "Codex process tree did not close its output streams"
            ) from error

    def _signal_process(self, state: _CodexRun, sig: int) -> None:
        if self._process_group_signals and state.process.pid is not None:
            try:
                os.killpg(state.process.pid, sig)
            except ProcessLookupError:
                return
            return
        if sig == signal.SIGTERM:
            state.process.terminate()
        elif sig == signal.SIGKILL:
            state.process.kill()
        else:
            state.process.send_signal(sig)

    def _active_state(self, run: RunHandle) -> _CodexRun:
        state = self._state(run)
        if state.cancelled or state.interrupted or state.collected_result is not None:
            raise RunNotActiveError(f"Codex run {run.id!r} is not active")
        return state

    def _state(self, run: RunHandle) -> _CodexRun:
        if run.driver != self._capabilities.name:
            raise UnknownRunError(
                f"Run {run.id!r} belongs to driver {run.driver!r}, not 'codex'"
            )
        try:
            return self._runs[run.id]
        except KeyError as error:
            raise UnknownRunError(f"Unknown Codex run {run.id!r}") from error


def render_execution_contract(execution: ExecutionContract) -> str:
    """Render only the worker-facing fields of an execution contract."""

    sections = [
        (
            "You are executing a bounded coding assignment under an external "
            "control plane."
        ),
        f"Job ID: {execution.job_id}",
        f"Role: {execution.role}",
        "",
        "Objective:",
        execution.objective,
        "",
        "Scope:",
        execution.scope,
        "",
        "Acceptance criteria:",
        _format_items(execution.acceptance_criteria),
        "",
        "Dependency results:",
        _format_mapping(execution.dependency_results),
        "",
        "Allowed filesystem scope:",
        _format_items(execution.allowed_filesystem_scope),
        "",
        "Checkpoint expectations:",
        execution.checkpoint_expectations,
        "",
        "Coordination mechanisms:",
        _format_items(execution.coordination_mechanisms),
        "",
        "Completion protocol:",
        execution.completion_protocol,
    ]
    if execution.resume is not None:
        sections.extend(
            (
                "",
                "Resume capsule:",
                f"- completed: {_inline_items(execution.resume.completed)}",
                f"- current: {_inline_items(execution.resume.current)}",
                f"- next: {_inline_items(execution.resume.next_steps)}",
                f"- commit: {execution.resume.commit or '(none)'}",
                f"- known failures: {_inline_items(execution.resume.known_failures)}",
                f"- decisions: {_inline_items(execution.resume.decisions)}",
            )
        )
    return "\n".join(sections).strip()


def _format_items(items: Sequence[str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def _format_mapping(items: Mapping[str, str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {key}: {items[key]}" for key in sorted(items))


def _inline_items(items: Sequence[str]) -> str:
    return "; ".join(items) if items else "(none)"


def _parse_output(output: str) -> _ParsedOutput:
    parsed = _ParsedOutput()
    plain_lines: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.malformed_line_count += 1
            plain_lines.append(line.strip())
            continue
        if not isinstance(event, dict):
            parsed.malformed_line_count += 1
            continue
        parsed.event_count += 1

        if event.get("type") == "thread.started":
            session_id = event.get("thread_id") or event.get("threadId")
            if isinstance(session_id, str):
                parsed.session_id = session_id

        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                parsed.summary = text

        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            _merge_usage(parsed.usage, event_usage)

    if not parsed.summary and plain_lines:
        parsed.summary = plain_lines[-1]
    return parsed


def _merge_usage(
    target: dict[str, JsonValue], incoming: Mapping[object, object]
) -> None:
    for raw_key, value in incoming.items():
        key = str(raw_key)
        previous = target.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and isinstance(previous, int | float)
            and not isinstance(previous, bool)
        ):
            target[key] = previous + value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            target[key] = value
