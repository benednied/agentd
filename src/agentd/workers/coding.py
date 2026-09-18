"""Opt-in bounded coding adapter for an operator-contained managed harness.

The transport still exposes only the existing typed lifecycle. This adapter is
not a sandbox: it refuses harnesses which do not declare the required enforced
credential/filesystem/network boundary. The stock SDK driver deliberately does
not declare credential isolation and cannot enable remote coding accidentally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentd.coding.models import RepositoryProfile, fingerprint
from agentd.domain.enums import ArtifactKind, QuotaUnit, RunOutcome
from agentd.domain.models import (
    ArtifactRef,
    CodingOperation,
    EffortEstimate,
    ExecutionContract,
    HarnessCapabilities,
    Job,
    QuotaBudget,
    RunHandle,
    RunObservation,
    RunResult,
    WorkspaceLease,
)
from agentd.harness.protocol import ManagedHarnessDriver
from agentd.workers.operations import (
    CommandRunner,
    GitRepositoryCache,
    GitWorkspace,
    OperationError,
    SubprocessCommandRunner,
    _git_environment,
)
from agentd.workspaces.git import GitWorkspaceManager

CODING_DRIVER = "remote-coding"
MAX_BUNDLE_BYTES = 262_144


@dataclass(slots=True)
class _CodingRun:
    driver: ManagedHarnessDriver
    handle: RunHandle
    workspace: GitWorkspace
    lease: WorkspaceLease
    manager: GitWorkspaceManager
    task: asyncio.Task[RunResult] | None = None


class CodingHarnessDriver:
    """Materialize pinned work orders and collect Git evidence, never publish."""

    def __init__(
        self,
        root: Path,
        profiles: Mapping[str, RepositoryProfile],
        harnesses: Mapping[str, ManagedHarnessDriver],
        *,
        runner: CommandRunner | None = None,
        account_pools: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profiles = dict(profiles)
        self.harnesses = dict(harnesses)
        self.account_pools = dict(account_pools or {})
        self.runner = runner or SubprocessCommandRunner()
        self._runs: dict[str, _CodingRun] = {}

    def capabilities(self) -> HarnessCapabilities:
        features = {"remote-coding"}
        models: set[str] = set()
        for name, driver in self.harnesses.items():
            caps = driver.capabilities()
            models.update(caps.models)
            features.add(f"harness-{name}")
            features.update(caps.features)
        features.update(
            f"repository-profile-{p.digest}" for p in self.profiles.values()
        )
        return HarnessCapabilities(
            CODING_DRIVER, frozenset(models), frozenset(features), steering=False
        )

    def _lease(self, run_id: str) -> Path:
        # Protocol identifiers are opaque strings, never filesystem authority.
        return self.root / hashlib.sha256(run_id.encode()).hexdigest()

    async def start(self, execution: ExecutionContract) -> RunHandle:
        raise OperationError("remote coding requires a durable managed run identity")

    async def start_managed(
        self, run_id: str, execution: ExecutionContract
    ) -> RunHandle:
        operation = execution.operation
        if not isinstance(operation, CodingOperation):
            raise OperationError("remote coding requires a typed work order")
        order = operation.work_order
        if execution.job_id != order.job_id:
            raise OperationError("coding job identity mismatch")
        profile = self.profiles.get(order.profile_id)
        if profile is None:
            raise OperationError("repository profile is not allowlisted")
        order.validate_profile(profile)
        driver = self.harnesses.get(order.harness)
        if driver is None or not isinstance(driver, ManagedHarnessDriver):
            raise OperationError("managed coding harness is unavailable")
        if self.account_pools.get(order.harness) != order.account_pool_id:
            raise OperationError("coding provider account does not match admission")
        required = {
            "credential-isolated",
            "restricted-workspace-write",
            f"network-{profile.network_policy}",
        }
        if not required.issubset(driver.capabilities().features):
            raise OperationError("coding harness containment is not enforced")
        if not set(order.required_capabilities).issubset(self.capabilities().features):
            raise OperationError("worker does not satisfy work-order capabilities")
        lease = self._lease(run_id)
        # The journal owns retries. A leftover lease is unresolved, never reset.
        lease.mkdir(mode=0o700, exist_ok=False)
        self._write(
            lease / "claim.json",
            {
                "run_id": run_id,
                "work_order": order.to_dict(),
                "fingerprint": fingerprint(order.to_dict()),
                "state": "claimed",
            },
        )
        cache = GitRepositoryCache(lease, frozenset({profile.clone_url}), self.runner)
        workspace = await cache.checkout(
            profile.clone_url, ArtifactRef(ArtifactKind.GIT_COMMIT, order.base_commit)
        )
        manager = GitWorkspaceManager(lease / "coding-worktrees")
        job = Job(
            id=order.job_id,
            project=order.repository,
            repository=str(workspace.path),
            objective=order.objective,
            quota_budget=QuotaBudget(
                order.expected_quota,
                maximum=order.maximum_quota,
                pool_id=order.account_pool_id,
                unit=QuotaUnit.TOKENS,
            ),
            effort=EffortEstimate(1, 1),
        )
        coding_lease = await asyncio.to_thread(manager.allocate, job, order.base_commit)
        workspace = replace(workspace, path=Path(coding_lease.working_directory))
        self._write(lease / "workspace.json", coding_lease.to_dict())
        # Controller fields outside the typed work order are not worker authority.
        local = replace(
            execution,
            objective=order.objective,
            acceptance_criteria=order.acceptance_criteria,
            working_directory=str(workspace.path),
            allowed_filesystem_scope=(str(workspace.path),),
            environment={},
            operation=operation,
            resume=None,
            dependency_results={},
            scope="Only the pinned repository workspace",
            role="implementer",
            coordination_mechanisms=(),
            completion_protocol=(
                "Leave file edits for trusted commit capture; "
                "do not commit, push or publish."
            ),
        )
        handle = await driver.start_managed(run_id, local)
        state = _CodingRun(driver, handle, workspace, coding_lease, manager)
        self._runs[run_id] = state
        state.task = asyncio.create_task(self._finish(run_id, state, execution))
        return RunHandle(run_id, CODING_DRIVER, handle.external_id)

    async def _finish(
        self, run_id: str, state: _CodingRun, execution: ExecutionContract
    ) -> RunResult:
        order = execution.operation.work_order
        result: RunResult | None = None
        try:
            async with asyncio.timeout(order.max_runtime_seconds):
                collection = asyncio.create_task(state.driver.collect(state.handle))
                try:
                    while not collection.done():
                        await asyncio.wait({collection}, timeout=0.1)
                        observation = state.driver.observe(run_id)
                        if (
                            observation is not None
                            and observation.usage is not None
                            and observation.usage.total_tokens >= order.maximum_quota
                        ):
                            await state.driver.cancel(state.handle)
                            raise TimeoutError("Coding token ceiling reached")
                    result = await collection
                finally:
                    if not collection.done():
                        collection.cancel()
                        await asyncio.gather(collection, return_exceptions=True)
            if result.outcome is RunOutcome.COMPLETED:
                if "live-token-usage" in state.driver.capabilities().features and (
                    result.metadata.get("telemetry_valid") is not True
                    or result.usage is None
                ):
                    raise OperationError("coding completion lacks valid usage evidence")
                evidence = await self._evidence(run_id, state, execution)
                result = replace(
                    result,
                    commit=evidence["result_commit"],
                    metadata={**result.metadata, "coding_evidence": evidence},
                    produced_artifacts=(),
                )
            else:
                result = replace(result, commit=None, produced_artifacts=())
        except TimeoutError:
            await state.driver.cancel(state.handle)
            result = self._failure_result(
                run_id,
                state,
                RunOutcome.CANCELLED,
                "Coding execution limit reached",
                result,
            )
        except asyncio.CancelledError:
            await state.driver.cancel(state.handle)
            result = self._failure_result(
                run_id, state, RunOutcome.CANCELLED, "Coding cancelled", result
            )
        except Exception:
            result = self._failure_result(
                run_id,
                state,
                RunOutcome.FAILED,
                "Trusted coding collection failed",
                result,
            )
        self._write(self._lease(run_id) / "result.json", result.to_dict())
        return result

    @staticmethod
    def _failure_result(
        run_id: str,
        state: _CodingRun,
        outcome: RunOutcome,
        summary: str,
        collected: RunResult | None = None,
    ) -> RunResult:
        if collected is not None:
            return replace(
                collected,
                outcome=outcome,
                summary=summary,
                commit=None,
                produced_artifacts=(),
            )
        observation = state.driver.observe(run_id)
        usage = observation.usage if observation is not None else None
        valid = getattr(observation, "telemetry_valid", False) and usage is not None
        return RunResult(
            outcome,
            summary,
            usage=usage,
            consumed_quota=float(usage.total_tokens) if usage is not None else 0,
            metadata={"telemetry_valid": bool(valid)},
        )

    async def _evidence(
        self, run_id: str, state: _CodingRun, execution: ExecutionContract
    ) -> dict[str, Any]:
        order = execution.operation.work_order
        workspace = state.workspace

        if workspace.path.is_symlink() or (workspace.path / ".git").is_symlink():
            raise OperationError("coding workspace identity changed")
        git_link = (workspace.path / ".git").read_text().strip()
        if not git_link.startswith("gitdir: "):
            raise OperationError("coding Git lease identity changed")
        git_dir = Path(git_link.removeprefix("gitdir: ")).resolve()
        if not git_dir.is_relative_to(workspace.mirror / "worktrees"):
            raise OperationError("coding Git directory escaped its lease")

        async def git(*args: str) -> bytes:
            output = await self.runner.run(
                (
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(workspace.path),
                    *args,
                ),
                environment=_git_environment() | {"GIT_NO_REPLACE_OBJECTS": "1"},
            )
            return output.stdout

        # Reuse the trusted handoff owner: the sandbox does not grant Git write
        # authority to the model. Hooks, filters and identity are controller policy.
        await asyncio.to_thread(state.manager.commit_changes, state.lease)
        # Read objects ourselves; model output cannot attest its own commit/tests.
        head = (await git("rev-parse", "HEAD^{commit}")).decode().strip()
        ArtifactRef(ArtifactKind.GIT_COMMIT, head)
        await git("merge-base", "--is-ancestor", order.base_commit, head)
        if await git("status", "--porcelain=v1", "--untracked-files=all"):
            raise OperationError("coding result contains uncommitted changes")
        if head == order.base_commit:
            raise OperationError("coding result contains no commit")
        bundle = self._lease(run_id) / "result.bundle"
        await git("bundle", "create", str(bundle), "HEAD", f"^{order.base_commit}")
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise OperationError("coding bundle requires operator collection")
        data = bundle.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "run_id": run_id,
            "job_id": order.job_id,
            "repository": order.repository,
            "base_commit": order.base_commit,
            "result_commit": head,
            "profile_digest": order.profile_digest,
            "source_revision": order.source_revision,
            "bundle_sha256": hashlib.sha256(data).hexdigest(),
            "bundle_chunks": [
                encoded[i : i + 16000] for i in range(0, len(encoded), 16000)
            ],
        }

    def load_terminal_result(self, run_id: str) -> RunResult | None:
        path = self._lease(run_id) / "result.json"
        if not path.is_file():
            return None
        return RunResult.from_dict(json.loads(path.read_text()))

    async def recover(
        self,
        run_id: str,
        execution: ExecutionContract,
        recovery_instruction: str = "",
    ) -> RunHandle:
        del execution, recovery_instruction
        if self.load_terminal_result(run_id) is None:
            raise OperationError("coding ownership is unresolved; restart is forbidden")
        return RunHandle(run_id, CODING_DRIVER)

    def observe(self, run_id: str) -> RunObservation | None:
        state = self._runs.get(run_id)
        return state.driver.observe(run_id) if state is not None else None

    def status(self, run: RunHandle) -> dict[str, Any]:
        result = self.load_terminal_result(run.id)
        return {
            "known": True,
            "terminal": result is not None,
            "result": result.to_dict() if result is not None else None,
        }

    async def collect(self, run: RunHandle) -> RunResult:
        result = self.load_terminal_result(run.id)
        if result is not None:
            return result
        state = self._runs.get(run.id)
        if state is None or state.task is None:
            raise OperationError("coding ownership is unresolved")
        return await asyncio.shield(state.task)

    async def steer(self, run: RunHandle, instruction: str) -> None:
        raise OperationError("remote coding does not accept out-of-order intent")

    async def interrupt(self, run: RunHandle) -> None:
        await self.cancel(run)

    async def cancel(self, run: RunHandle) -> None:
        state = self._runs.get(run.id)
        if state is None or state.task is None:
            raise OperationError("coding ownership is unresolved")
        if not state.task.done():
            await state.driver.cancel(state.handle)
            state.task.cancel()
            with suppress(asyncio.CancelledError):
                await state.task
            if self.load_terminal_result(run.id) is None:
                self._write(
                    self._lease(run.id) / "result.json",
                    self._failure_result(
                        run.id,
                        state,
                        RunOutcome.CANCELLED,
                        "Coding cancelled before collection",
                    ).to_dict(),
                )

    def release(self, run_id: str) -> None:
        """Explicit administrative retention acknowledgement; never auto-delete."""
        if self.load_terminal_result(run_id) is None:
            raise OperationError("cannot release an unresolved coding lease")
        # Keep claim/result evidence; remove only model workspace and bundle.
        import shutil

        lease = self._lease(run_id)
        for name in ("coding-worktrees", "worktrees", "mirrors"):
            if (lease / name).exists():
                shutil.rmtree(lease / name, ignore_errors=False)
        (lease / "result.bundle").unlink(missing_ok=True)

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
