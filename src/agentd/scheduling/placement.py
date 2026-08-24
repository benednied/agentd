"""Capability-driven worker-node and harness placement policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from agentd.domain.enums import NodeState
from agentd.domain.models import HarnessCapabilities, Job, ResourceVector, WorkerNode

_TRUTHY_LABEL_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class Placement:
    """A compatible node, harness, and model choice with auditable scores."""

    node_id: str
    harness: str
    model_class: str
    harness_preference: int
    resource_waste: float


def _has_node_capability(node: WorkerNode, capability: str) -> bool:
    if capability in node.capabilities:
        return True
    return node.labels.get(capability, "").casefold() in _TRUTHY_LABEL_VALUES


def _matches_execution_requirements(job: Job, node: WorkerNode) -> bool:
    requirements = job.execution_requirements
    if requirements.os is not None and node.labels.get("os") != requirements.os:
        return False
    if requirements.arch is not None and node.labels.get("arch") != requirements.arch:
        return False
    if any(node.labels.get(key) != value for key, value in requirements.labels.items()):
        return False
    if requirements.browser and not _has_node_capability(node, "browser"):
        return False
    return not (requirements.desktop and not _has_node_capability(node, "desktop"))


def _select_model(job: Job, harness: HarnessCapabilities) -> str | None:
    if job.preferred_model_class in harness.models:
        return job.preferred_model_class
    if job.minimum_model_class in harness.models:
        return job.minimum_model_class
    return None


def _available_resources(node: WorkerNode) -> ResourceVector | None:
    if not node.allocated.fits_within(node.capacity):
        return None
    return node.available


def _normalized_waste(
    node: WorkerNode,
    available: ResourceVector,
    requested: ResourceVector,
) -> float:
    remaining = available - requested
    capacity_values = (
        node.capacity.cpu,
        node.capacity.ram_gb,
        float(node.capacity.gpu_count),
        node.capacity.vram_gb,
    )
    remaining_values = (
        remaining.cpu,
        remaining.ram_gb,
        float(remaining.gpu_count),
        remaining.vram_gb,
    )
    return sum(
        leftover / capacity
        for leftover, capacity in zip(remaining_values, capacity_values, strict=True)
        if capacity > 0
    )


def _harness_preference(job: Job, harness_name: str) -> int:
    try:
        return job.preferred_harnesses.index(harness_name)
    except ValueError:
        try:
            allowed_rank = job.allowed_harnesses.index(harness_name)
        except ValueError:  # pragma: no cover - caller filters allowed harnesses
            allowed_rank = len(job.allowed_harnesses)
        return len(job.preferred_harnesses) + allowed_rank


def compatible_placements(
    job: Job,
    nodes: Iterable[WorkerNode],
    harnesses: Mapping[str, HarnessCapabilities],
) -> tuple[Placement, ...]:
    """Return compatible placements in deterministic best-fit order.

    Explicit harness preference is respected first. Within an equally preferred
    harness, the node leaving the least normalized resource headroom is chosen;
    stable IDs break exact ties.
    """

    placements: list[Placement] = []
    allowed_harnesses = frozenset(job.allowed_harnesses)

    for node in nodes:
        if node.state is not NodeState.ONLINE:
            continue
        if not _matches_execution_requirements(job, node):
            continue
        available = _available_resources(node)
        if available is None or not job.resources.fits_within(available):
            continue

        for harness_name in sorted(node.harnesses & allowed_harnesses):
            harness = harnesses.get(harness_name)
            if harness is None or harness.name != harness_name:
                continue
            model_class = _select_model(job, harness)
            if model_class is None:
                continue
            available_capabilities = node.capabilities | harness.features
            if not job.required_capabilities <= available_capabilities:
                continue
            placements.append(
                Placement(
                    node_id=node.id,
                    harness=harness_name,
                    model_class=model_class,
                    harness_preference=_harness_preference(job, harness_name),
                    resource_waste=_normalized_waste(node, available, job.resources),
                )
            )

    return tuple(
        sorted(
            placements,
            key=lambda placement: (
                placement.harness_preference,
                placement.resource_waste,
                placement.node_id,
                placement.harness,
                placement.model_class,
            ),
        )
    )


def select_placement(
    job: Job,
    nodes: Iterable[WorkerNode],
    harnesses: Mapping[str, HarnessCapabilities],
) -> Placement | None:
    """Return the best compatible placement, or ``None`` when none fits."""

    placements = compatible_placements(job, nodes, harnesses)
    return placements[0] if placements else None
