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

from agentd.coding.models import (
    CodingWorkOrder,
    RepositoryProfile,
    exact_commit,
    fingerprint,
)
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
    TokenUsage,
    WorkspaceLease,
)
from agentd.harness.protocol import ManagedHarnessDriver
from agentd.workers.coding_runtime import CodingPreparationError
from agentd.workers.operations import (
    CommandRunner,
    GitRepositoryCache,
    GitWorkspace,
    OperationError,
    SubprocessCommandRunner,
    _git_environment,
)
from agentd.workspaces.git import _RUNTIME_SCRATCH_PATHS, GitWorkspaceManager

CODING_DRIVER = "remote-coding"
MAX_BUNDLE_BYTES = 262_144


class CodingOwnershipUnresolved(OperationError):
    """A stop request did not prove that the provider run is terminal."""


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
        self._recovery_locks: dict[str, asyncio.Lock] = {}

    def capabilities(self) -> HarnessCapabilities:
        features = {"remote-coding"}
        features.add("coding-checkpoints")
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

    @staticmethod
    def _workspace_manager(root: Path) -> GitWorkspaceManager:
        return GitWorkspaceManager(
            root,
            process_environment=_git_environment()
            | {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": os.devnull,
            },
            trusted_git=True,
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
        resume_commit = order.base_commit
        if order.resume_from_run_id is not None:
            checkpoint = self._read_checkpoint(order.resume_from_run_id, order)
            resume_commit = checkpoint["result_commit"]
            if resume_commit != order.base_commit:
                await self.runner.run(
                    (
                        "git",
                        "-c",
                        "core.hooksPath=" + os.devnull,
                        "-c",
                        "core.fsmonitor=false",
                        "--git-dir",
                        str(workspace.mirror),
                        "fetch",
                        "--no-tags",
                        "--",
                        str(self._lease(order.resume_from_run_id) / "result.bundle"),
                        resume_commit,
                    ),
                    environment=_git_environment(),
                )
        manager = self._workspace_manager(lease / "coding-worktrees")
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
        coding_lease = await asyncio.to_thread(manager.allocate, job, resume_commit)
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
        try:
            handle = await driver.start_managed(run_id, local)
        except CodingPreparationError:
            self._write(
                lease / "result.json",
                RunResult(
                    RunOutcome.FAILED,
                    "Worker preparation failed before provider start",
                    usage=TokenUsage(),
                    metadata={
                        "provider_started": False,
                        "preparation_failure": True,
                        "telemetry_valid": True,
                    },
                ).to_dict(),
            )
            return RunHandle(run_id, CODING_DRIVER)
        state = _CodingRun(driver, handle, workspace, coding_lease, manager)
        self._runs[run_id] = state
        state.task = asyncio.create_task(self._finish(run_id, state, execution))
        return RunHandle(run_id, CODING_DRIVER, handle.external_id)

    async def _finish(
        self, run_id: str, state: _CodingRun, execution: ExecutionContract
    ) -> RunResult:
        assert isinstance(execution.operation, CodingOperation)
        order = execution.operation.work_order
        result: RunResult | None = None
        try:
            async with asyncio.timeout(order.max_runtime_seconds):
                collection = asyncio.create_task(state.driver.collect(state.handle))
                try:
                    while not collection.done():
                        await asyncio.wait({collection}, timeout=0.1)
                        if collection.done():
                            break
                        observation = state.driver.observe(run_id)
                        if (
                            observation is not None
                            and observation.usage is not None
                            and observation.usage.total_tokens
                            >= order.maximum_quota - order.prior_consumed_quota
                        ):
                            if observation.terminal:
                                break
                            if await self._cancel_active(run_id, state):
                                raise TimeoutError("Coding token ceiling reached")
                            break
                    result = await collection
                finally:
                    if not collection.done():
                        collection.cancel()
                        await asyncio.gather(collection, return_exceptions=True)
            return await self._finalize_result(run_id, state, execution, result)
        except TimeoutError:
            await self._cancel_active(run_id, state)
            result = self._failure_result(
                run_id,
                state,
                RunOutcome.CANCELLED,
                "Coding execution limit reached",
                result,
            )
        except asyncio.CancelledError:
            await self._cancel_active(run_id, state)
            result = self._failure_result(
                run_id, state, RunOutcome.CANCELLED, "Coding cancelled", result
            )
        except CodingOwnershipUnresolved:
            raise
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

    async def _cancel_active(self, run_id: str, state: _CodingRun) -> bool:
        observation = state.driver.observe(run_id)
        if observation is not None and observation.terminal:
            return False
        try:
            await state.driver.cancel(state.handle)
        except Exception as error:
            observation = state.driver.observe(run_id)
            if observation is not None and observation.terminal:
                return False
            # Absence of a live process is not proof of terminal ownership.
            raise CodingOwnershipUnresolved(
                "coding stop ownership is unresolved"
            ) from error
        if "live-token-usage" in state.driver.capabilities().features:
            try:
                await asyncio.wait_for(state.driver.collect(state.handle), timeout=5)
            except Exception as error:
                raise CodingOwnershipUnresolved(
                    "coding stop lacks terminal evidence"
                ) from error
            observation = state.driver.observe(run_id)
            if observation is None or not observation.terminal:
                raise CodingOwnershipUnresolved(
                    "coding stop lacks terminal observation"
                )
        return True

    async def _finalize_result(
        self,
        run_id: str,
        state: _CodingRun,
        execution: ExecutionContract,
        result: RunResult,
    ) -> RunResult:
        assert isinstance(execution.operation, CodingOperation)
        if result.metadata.get("telemetry_valid") is True and result.usage is not None:
            # The SDK records usage in its worker ledger and returns a zero
            # scalar to avoid double charging there. Across transport this is a
            # cumulative final reading; controller release uses max(previous,
            # final) and must retain it even if the last observation was lost.
            consumed = float(result.usage.total_tokens)
            result = replace(
                result,
                consumed_quota=max(result.consumed_quota, consumed),
                metadata={
                    **result.metadata,
                    "quota_ceiling_exceeded": consumed
                    > execution.operation.work_order.maximum_quota,
                },
            )
        if result.outcome is RunOutcome.COMPLETED:
            if "live-token-usage" in state.driver.capabilities().features and (
                result.metadata.get("telemetry_valid") is not True
                or result.usage is None
            ):
                raise OperationError("coding completion lacks valid usage evidence")
            evidence = await self._evidence(run_id, state, execution)
            usage = result.usage
            evidence["quota_ceiling_exceeded"] = bool(
                usage is not None
                and usage.total_tokens > execution.operation.work_order.maximum_quota
            )
            result = replace(
                result,
                commit=evidence["result_commit"],
                metadata={**result.metadata, "coding_evidence": evidence},
                produced_artifacts=(),
            )
        else:
            result = replace(result, commit=None, produced_artifacts=())
        self._write(self._lease(run_id) / "result.json", result.to_dict())
        return result

    def reconcile_preparation_failure(
        self, run_id: str, *, deployed_revision: str
    ) -> RunResult:
        """Explicit admin-only legacy repair after the worker has quiesced.

        Never called by STATUS and never starts a provider. Existing claims,
        ledgers and workspace records are retained for audit.
        """
        exact_commit(deployed_revision)
        persisted = self.load_terminal_result(run_id)
        if persisted is not None:
            return persisted
        if run_id in self._runs:
            raise OperationError("preparation repair requires a quiescent worker")
        root = self._lease(run_id)
        claim = json.loads((root / "claim.json").read_text())
        order = CodingWorkOrder.from_dict(claim["work_order"])
        if claim["run_id"] != run_id or claim["fingerprint"] != fingerprint(
            order.to_dict()
        ):
            raise OperationError("coding preparation claim identity mismatch")
        order.validate_profile(self.profiles[order.profile_id])
        if self.account_pools.get(order.harness) != order.account_pool_id:
            raise OperationError("coding preparation account mismatch")
        lease = WorkspaceLease.from_dict(
            json.loads((root / "workspace.json").read_text())
        )
        if lease.job_id != order.job_id or lease.base_ref != order.base_commit:
            raise OperationError("coding preparation workspace mismatch")
        proof = getattr(
            self.harnesses[order.harness], "prove_preparation_failure", None
        )
        if proof is None:
            raise OperationError("coding harness cannot prove pre-provider failure")
        result = proof(run_id, order, lease)
        if result.metadata.get("provider_started") is not False:
            raise OperationError("preparation proof did not exclude provider start")
        result = replace(
            result,
            metadata={
                **result.metadata,
                "preparation_deployed_revision": deployed_revision,
            },
        )
        self._write(root / "result.json", result.to_dict())
        return result

    async def recover_terminal(self, run_id: str) -> RunResult | None:
        """Finalize a proven durable SDK terminal result without starting a turn."""
        lock = self._recovery_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            persisted = self._raw_terminal_result(run_id)
            if persisted is not None:
                if (self._lease(run_id) / "checkpoint-request.json").is_file():
                    live = self._runs.get(run_id)
                    if live and live.task and not live.task.done():
                        return None
                    await self.capture_checkpoint(run_id)
                return self.load_terminal_result(run_id)
            live = self._runs.get(run_id)
            if live is not None and live.task is not None and not live.task.done():
                return None
            root = self._lease(run_id)
            if (
                not (root / "claim.json").is_file()
                or not (root / "workspace.json").is_file()
            ):
                return None
            claim = json.loads((root / "claim.json").read_text())
            order = CodingWorkOrder.from_dict(claim["work_order"])
            if claim["run_id"] != run_id or claim["fingerprint"] != fingerprint(
                order.to_dict()
            ):
                raise OperationError("coding recovery identity mismatch")
            profile = self.profiles[order.profile_id]
            order.validate_profile(profile)
            if self.account_pools.get(order.harness) != order.account_pool_id:
                raise OperationError("coding recovery account mismatch")
            driver = self.harnesses[order.harness]
            observation = driver.observe(run_id)
            if (
                observation is None
                or not observation.terminal
                or observation.result is None
            ):
                return None
            if observation.run_id != run_id:
                raise OperationError("coding recovery observation identity mismatch")
            lease = WorkspaceLease.from_dict(
                json.loads((root / "workspace.json").read_text())
            )
            if (
                lease.job_id != order.job_id
                or lease.base_ref
                != (
                    self._read_checkpoint(order.resume_from_run_id, order)[
                        "result_commit"
                    ]
                    if order.resume_from_run_id is not None
                    else order.base_commit
                )
                or not Path(lease.working_directory)
                .resolve()
                .is_relative_to(root / "coding-worktrees")
            ):
                raise OperationError("coding recovery workspace identity mismatch")
            mirror = (
                root
                / "mirrors"
                / hashlib.sha256(profile.clone_url.encode()).hexdigest()
            )
            workspace = GitWorkspace(
                profile.clone_url,
                ArtifactRef(ArtifactKind.GIT_COMMIT, order.base_commit),
                Path(lease.working_directory),
                mirror,
            )
            state = _CodingRun(
                driver,
                RunHandle(run_id, order.harness),
                workspace,
                lease,
                self._workspace_manager(root / "coding-worktrees"),
            )
            execution = ExecutionContract(
                order.job_id,
                order.objective,
                "workspace",
                order.acceptance_criteria,
                {},
                "implementer",
                (lease.working_directory,),
                "",
                (),
                "",
                lease.working_directory,
                {},
                "standard",
                operation=CodingOperation(order),
            )
            return await self._finalize_result(
                run_id, state, execution, observation.result
            )

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
                consumed_quota=max(
                    collected.consumed_quota,
                    float(collected.usage.total_tokens)
                    if collected.metadata.get("telemetry_valid") is True
                    and collected.usage is not None
                    else 0,
                ),
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
        self,
        run_id: str,
        state: _CodingRun,
        execution: ExecutionContract,
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        assert isinstance(execution.operation, CodingOperation)
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

        # Worker-owned per-run mirror configuration is policy, never model output.
        # Reset it before any trusted Git reads/staging so local includes,
        # filters, pagers and fsmonitor cannot become controller code execution.
        # This also safely handles terminal recovery from older leases which
        # predate metadata baselines: no model-authored config is interpreted.
        config = workspace.mirror / "config"
        if workspace.mirror.is_symlink() or config.is_symlink() or not config.is_file():
            raise OperationError("coding mirror configuration identity changed")
        for anchored in (workspace.path, Path(state.lease.repository)):
            link_path = anchored / ".git"
            if anchored.is_symlink() or link_path.is_symlink():
                raise OperationError("coding worktree anchor changed")
            link = link_path.read_text().strip()
            if not link.startswith("gitdir: ") or not Path(
                link[8:]
            ).resolve().is_relative_to(workspace.mirror / "worktrees"):
                raise OperationError("coding worktree anchor escaped its mirror")
        original_digest = hashlib.sha256(config.read_bytes()).hexdigest()
        safe_config = "[core]\nrepositoryformatversion = 0\nbare = true\n"
        if len(order.base_commit) == 64:
            safe_config = (
                "[core]\nrepositoryformatversion = 1\nbare = true\n"
                "[extensions]\nobjectFormat = sha256\n"
            )
        temporary = config.with_name("config.agentd-tmp")
        if temporary.is_symlink():
            raise OperationError("coding config staging path is a symlink")
        temporary.write_text(safe_config)
        temporary.replace(config)
        self._write(
            self._lease(run_id) / "git-policy.json",
            {
                "original_config_sha256": original_digest,
                "policy": "no-local-hooks-filters-fsmonitor-includes",
            },
        )

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
                environment=_git_environment()
                | {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull},
            )
            return output.stdout

        # Reuse the trusted handoff owner: the sandbox does not grant Git write
        # authority to the model. Hooks, filters and identity are controller policy.
        await asyncio.to_thread(state.manager.commit_changes, state.lease)
        # Read objects ourselves; model output cannot attest its own commit/tests.
        head = (await git("rev-parse", "HEAD^{commit}")).decode().strip()
        ArtifactRef(ArtifactKind.GIT_COMMIT, head)
        await git("merge-base", "--is-ancestor", order.base_commit, head)
        # Match the existing trusted commit owner's scratch exclusions. Pytest
        # can fall back to a lease-local temp directory inside the sandbox.
        if await git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            *_RUNTIME_SCRATCH_PATHS,
        ):
            raise OperationError("coding result contains uncommitted changes")
        if head == order.base_commit and not allow_empty:
            raise OperationError("coding result contains no commit")
        bundle = self._lease(run_id) / "result.bundle"
        if head == order.base_commit:
            bundle.write_bytes(b"")
        else:
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

    def _raw_terminal_result(self, run_id: str) -> RunResult | None:
        path = self._lease(run_id) / "result.json"
        if not path.is_file():
            return None
        return RunResult.from_dict(json.loads(path.read_text()))

    def load_terminal_result(self, run_id: str) -> RunResult | None:
        result = self._raw_terminal_result(run_id)
        root = self._lease(run_id)
        path = root / "checkpoint.json"
        if (root / "checkpoint-request.json").is_file() and not path.is_file():
            return None
        if result is not None and path.is_file():
            checkpoint = json.loads(path.read_text())
            result = replace(
                result,
                metadata={
                    **result.metadata,
                    "coding_checkpoint": {
                        key: value
                        for key, value in checkpoint.items()
                        if key != "bundle_chunks"
                    },
                },
            )
        return result

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
        # Persist intent first. A retry/restart can finish capture without
        # starting another provider turn or confusing interruption with cancel.
        if self._raw_terminal_result(run.id) is not None:
            if (self._lease(run.id) / "checkpoint-request.json").is_file():
                await self.capture_checkpoint(run.id)
            return
        self._write(self._lease(run.id) / "checkpoint-request.json", {"run_id": run.id})
        await self.cancel(run)
        await self.capture_checkpoint(run.id)

    def _read_checkpoint(self, run_id: str, order: CodingWorkOrder) -> dict[str, Any]:
        root = self._lease(run_id)
        checkpoint = json.loads((root / "checkpoint.json").read_text())
        claim = json.loads((root / "claim.json").read_text())
        previous = CodingWorkOrder.from_dict(claim["work_order"])
        if claim["run_id"] != run_id or claim["fingerprint"] != fingerprint(
            previous.to_dict()
        ):
            raise OperationError("checkpoint claim identity mismatch")
        for field in (
            "job_id",
            "repository",
            "profile_digest",
            "source_revision",
            "base_commit",
            "objective",
            "harness",
            "account_pool_id",
        ):
            if getattr(previous, field) != getattr(order, field):
                raise OperationError("checkpoint belongs to different work")
        if checkpoint["run_id"] != run_id or checkpoint["job_id"] != order.job_id:
            raise OperationError("checkpoint identity mismatch")
        if order.prior_consumed_quota < checkpoint["cumulative_quota"]:
            raise OperationError("continuation would erase prior usage")
        if self.load_terminal_result(run_id) is None:
            raise OperationError("checkpoint ownership remains unresolved")
        bundle = root / "result.bundle"
        if (
            bundle.is_symlink()
            or hashlib.sha256(bundle.read_bytes()).hexdigest()
            != checkpoint["bundle_sha256"]
        ):
            raise OperationError("checkpoint bundle changed")
        return checkpoint

    async def capture_checkpoint(self, run_id: str) -> dict[str, Any]:
        """Capture a proven stopped lease; also supports audited legacy recovery.

        Administrative API only: never starts a model or rewrites the original
        terminal result. The sidecar preserves checkpoint capture across crashes.
        """
        root = self._lease(run_id)
        path = root / "checkpoint.json"
        if path.is_file():
            return json.loads(path.read_text())
        result = self._raw_terminal_result(run_id)
        live = self._runs.get(run_id)
        if result is None or (live and live.task and not live.task.done()):
            raise CodingOwnershipUnresolved("checkpoint requires terminal ownership")
        if result.metadata.get("telemetry_valid") is not True or result.usage is None:
            raise OperationError("checkpoint requires reconciled token usage")
        claim = json.loads((root / "claim.json").read_text())
        order = CodingWorkOrder.from_dict(claim["work_order"])
        if claim["run_id"] != run_id or claim["fingerprint"] != fingerprint(
            order.to_dict()
        ):
            raise OperationError("checkpoint claim identity mismatch")
        order.validate_profile(self.profiles[order.profile_id])
        lease = WorkspaceLease.from_dict(
            json.loads((root / "workspace.json").read_text())
        )
        if lease.job_id != order.job_id or not Path(
            lease.working_directory
        ).resolve().is_relative_to(root / "coding-worktrees"):
            raise OperationError("checkpoint workspace identity mismatch")
        driver = self.harnesses[order.harness]
        observation = driver.observe(run_id)
        if observation is not None and not observation.terminal:
            raise CodingOwnershipUnresolved("checkpoint provider is still active")
        mirror = (
            root
            / "mirrors"
            / hashlib.sha256(
                self.profiles[order.profile_id].clone_url.encode()
            ).hexdigest()
        )
        workspace = GitWorkspace(
            self.profiles[order.profile_id].clone_url,
            ArtifactRef(ArtifactKind.GIT_COMMIT, order.base_commit),
            Path(lease.working_directory),
            mirror,
        )
        state = _CodingRun(
            driver,
            RunHandle(run_id, order.harness),
            workspace,
            lease,
            self._workspace_manager(root / "coding-worktrees"),
        )
        execution = ExecutionContract(
            order.job_id,
            order.objective,
            "checkpoint",
            (),
            {},
            "implementer",
            (lease.working_directory,),
            "",
            (),
            "",
            lease.working_directory,
            {},
            "standard",
            operation=CodingOperation(order),
        )
        evidence = await self._evidence(run_id, state, execution, allow_empty=True)
        checkpoint = {
            **evidence,
            "cumulative_quota": order.prior_consumed_quota + result.usage.total_tokens,
        }
        self._write(path, checkpoint)
        return checkpoint

    async def cancel(self, run: RunHandle) -> None:
        state = self._runs.get(run.id)
        if state is None or state.task is None:
            raise OperationError("coding ownership is unresolved")
        if state.task.done() and self.load_terminal_result(run.id) is None:
            if await self.recover_terminal(run.id) is None:
                raise CodingOwnershipUnresolved("coding stop ownership is unresolved")
            return
        if not state.task.done():
            if not await self._cancel_active(run.id, state):
                await asyncio.shield(state.task)
                return
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

    async def close(self) -> None:
        """Stop local live tasks while retaining their terminal lease evidence."""
        for run_id in tuple(self._runs):
            await self.cancel(RunHandle(run_id, CODING_DRIVER))

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
