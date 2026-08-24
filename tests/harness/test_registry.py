import pytest

from agentd.domain.models import HarnessCapabilities
from agentd.harness import (
    DriverAlreadyRegisteredError,
    DriverRegistry,
    FakeHarnessDriver,
    HarnessDriver,
    UnknownDriverError,
)


def test_fake_driver_implements_protocol() -> None:
    assert isinstance(FakeHarnessDriver(), HarnessDriver)


def test_registry_discovers_drivers_in_stable_name_order() -> None:
    zeta = FakeHarnessDriver(
        capabilities=HarnessCapabilities(
            name="zeta",
            models=frozenset({"standard"}),
            features=frozenset(),
        )
    )
    alpha = FakeHarnessDriver(
        capabilities=HarnessCapabilities(
            name="alpha",
            models=frozenset({"cheap"}),
            features=frozenset({"steering"}),
        )
    )

    registry = DriverRegistry((zeta, alpha))

    assert registry.names() == ("alpha", "zeta")
    assert tuple(item.name for item in registry.capabilities()) == (
        "alpha",
        "zeta",
    )
    assert registry.get("alpha") is alpha
    assert "zeta" in registry
    assert len(registry) == 2


def test_registry_rejects_accidental_replacement() -> None:
    original = FakeHarnessDriver()
    replacement = FakeHarnessDriver()
    registry = DriverRegistry((original,))

    with pytest.raises(DriverAlreadyRegisteredError, match="already registered"):
        registry.register(replacement)

    registry.register(replacement, replace=True)
    assert registry.get("fake") is replacement


def test_registry_reports_unknown_names() -> None:
    registry = DriverRegistry()

    with pytest.raises(UnknownDriverError, match="missing"):
        registry.get("missing")
    with pytest.raises(UnknownDriverError, match="missing"):
        registry.remove("missing")
