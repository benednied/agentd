from agentd.domain.enums import NodeState
from agentd.domain.models import (
    ExecutionRequirements,
    HarnessCapabilities,
    ResourceVector,
)
from agentd.scheduling.placement import compatible_placements, select_placement


def test_selects_preferred_harness_then_deterministic_best_fit(
    job_factory, node_factory
):
    job = job_factory(
        preferred_harnesses=("fake", "local"),
        allowed_harnesses=("fake", "local"),
        resources=ResourceVector(cpu=2, ram_gb=4),
    )
    large = node_factory(
        id="large",
        capacity=ResourceVector(cpu=16, ram_gb=32),
        harnesses=frozenset({"fake", "local"}),
    )
    small_b = node_factory(
        id="small-b",
        capacity=ResourceVector(cpu=4, ram_gb=8),
        harnesses=frozenset({"fake"}),
    )
    small_a = node_factory(
        id="small-a",
        capacity=ResourceVector(cpu=4, ram_gb=8),
        harnesses=frozenset({"fake"}),
    )
    harnesses = {
        "fake": HarnessCapabilities(
            name="fake", models=frozenset({"standard"}), features=frozenset()
        ),
        "local": HarnessCapabilities(
            name="local", models=frozenset({"standard"}), features=frozenset()
        ),
    }

    placements = compatible_placements(job, [large, small_b, small_a], harnesses)

    assert [(item.node_id, item.harness) for item in placements] == [
        ("small-a", "fake"),
        ("small-b", "fake"),
        ("large", "fake"),
        ("large", "local"),
    ]
    assert (
        select_placement(job, reversed([large, small_b, small_a]), harnesses)
        == (placements[0])
    )


def test_matches_node_harness_model_features_and_environment(job_factory, node_factory):
    job = job_factory(
        execution_requirements=ExecutionRequirements(
            os="linux",
            arch="arm64",
            browser=True,
            labels={"pool": "trusted"},
        ),
        required_capabilities=frozenset({"patch", "browser"}),
        preferred_model_class="premium",
        minimum_model_class="standard",
        resources=ResourceVector(cpu=2, ram_gb=4, gpu_count=1, vram_gb=8),
    )
    node = node_factory(
        labels={"os": "linux", "arch": "arm64", "pool": "trusted"},
        capacity=ResourceVector(cpu=8, ram_gb=32, gpu_count=1, vram_gb=16),
        capabilities=frozenset({"browser"}),
    )
    harnesses = {
        "fake": HarnessCapabilities(
            name="fake",
            models=frozenset({"standard"}),
            features=frozenset({"patch"}),
        )
    }

    placement = select_placement(job, [node], harnesses)

    assert placement is not None
    assert placement.model_class == "standard"


def test_rejects_offline_mismatched_or_insufficient_nodes(job_factory, node_factory):
    job = job_factory(
        execution_requirements=ExecutionRequirements(os="linux", desktop=True),
        required_capabilities=frozenset({"patch"}),
        resources=ResourceVector(cpu=4, ram_gb=8),
    )
    driver = HarnessCapabilities(
        name="fake", models=frozenset({"standard"}), features=frozenset({"patch"})
    )
    nodes = [
        node_factory(id="offline", state=NodeState.OFFLINE),
        node_factory(id="wrong-os", labels={"os": "macos", "arch": "arm64"}),
        node_factory(id="no-desktop"),
        node_factory(
            id="too-small",
            capabilities=frozenset({"desktop"}),
            capacity=ResourceVector(cpu=2, ram_gb=4),
        ),
    ]

    assert select_placement(job, nodes, {"fake": driver}) is None


def test_rejects_unregistered_disallowed_and_incapable_harnesses(
    job_factory, node_factory
):
    job = job_factory(required_capabilities=frozenset({"steer"}))
    node = node_factory(harnesses=frozenset({"fake", "other"}))

    assert select_placement(job, [node], {}) is None
    assert (
        select_placement(
            job,
            [node],
            {
                "fake": HarnessCapabilities(
                    name="fake",
                    models=frozenset({"standard"}),
                    features=frozenset(),
                )
            },
        )
        is None
    )
