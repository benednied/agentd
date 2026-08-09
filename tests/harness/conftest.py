from collections.abc import Iterator

import pytest

from agentd.domain.models import ExecutionContract, ResumeCapsule


@pytest.fixture
def execution_contract() -> ExecutionContract:
    return ExecutionContract(
        job_id="job-1",
        objective="Implement the assigned change safely.",
        scope="Only the harness package.",
        acceptance_criteria=("Focused tests pass", "No scheduler policy leaks"),
        dependency_results={"dep-b": "second", "dep-a": "first"},
        role="implementer",
        allowed_filesystem_scope=("/workspace", "/shared"),
        checkpoint_expectations="Checkpoint before interruption.",
        coordination_mechanisms=("checkpoint", "request_review"),
        completion_protocol="Report the result and commit.",
        working_directory="/workspace",
        environment={"TASK_TOKEN": "secret-value"},
        model_class="standard",
        resume=ResumeCapsule(
            completed=("schema",),
            current=("adapter",),
            next_steps=("tests",),
            commit="abc123",
            known_failures=("test_timeout",),
            decisions=("use subprocess argv",),
        ),
    )


@pytest.fixture
def deterministic_ids() -> Iterator[str]:
    return iter(("run-1", "run-2", "run-3"))
