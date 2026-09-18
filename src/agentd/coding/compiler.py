"""Compile approved source intent using controller-owned repository policy."""

from __future__ import annotations

from dataclasses import dataclass

from agentd.coding.models import CodingWorkOrder, RepositoryProfile, exact_commit
from agentd.domain.enums import QoSClass, QuotaUnit
from agentd.domain.models import (
    CodingOperation,
    EffortEstimate,
    Job,
    QuotaBudget,
)
from agentd.intake.models import SourceIssue


@dataclass(frozen=True, slots=True)
class CodingJobCompiler:
    profile: RepositoryProfile
    base_commit: str
    budget: QuotaBudget
    effort: EffortEstimate
    harness: str = "codex"
    model_class: str = "standard"
    acceptance_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        exact_commit(self.base_commit)
        if self.budget.maximum is None or self.budget.unit is not QuotaUnit.TOKENS:
            raise ValueError("coding policy requires a bounded token budget")
        if self.harness not in self.profile.harnesses:
            raise ValueError("coding policy harness is not allowed by profile")

    def __call__(self, issue: SourceIssue) -> Job:
        if issue.repository != self.profile.repository:
            raise ValueError("source repository does not match profile")
        assert self.budget.maximum is not None
        work_order = CodingWorkOrder(
            job_id=issue.job_id,
            repository=self.profile.repository,
            profile_id=self.profile.id,
            profile_version=self.profile.version,
            profile_digest=self.profile.digest,
            base_commit=self.base_commit,
            source_revision=issue.revision,
            objective=issue.title + "\n\n" + issue.body,
            harness=self.harness,
            account_pool_id=self.budget.pool_id,
            expected_quota=self.budget.expected_path,
            maximum_quota=self.budget.maximum,
            max_runtime_seconds=self.profile.max_runtime_seconds,
            acceptance_criteria=self.acceptance_criteria,
            required_capabilities=self.profile.required_capabilities,
        )
        work_order.validate_profile(self.profile)
        return Job(
            id=issue.job_id,
            project=issue.repository,
            repository=self.profile.clone_url,
            base_ref=self.base_commit,
            objective=work_order.objective,
            quota_budget=self.budget,
            effort=self.effort,
            qos=QoSClass.SCAVENGER,
            allowed_harnesses=("remote-coding",),
            preferred_harnesses=("remote-coding",),
            preferred_model_class=self.model_class,
            minimum_model_class=self.model_class,
            acceptance_criteria=self.acceptance_criteria,
            required_capabilities=frozenset(self.profile.required_capabilities)
            | {
                "remote-coding",
                f"harness-{self.harness}",
                f"repository-profile-{self.profile.digest}",
            },
            operation=CodingOperation(work_order),
        )
