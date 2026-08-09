"""Registry for capability-discoverable harness drivers."""

from collections.abc import Iterable

from agentd.domain.models import HarnessCapabilities
from agentd.harness.errors import (
    DriverAlreadyRegisteredError,
    UnknownDriverError,
)
from agentd.harness.protocol import HarnessDriver


class DriverRegistry:
    """Own driver discovery without making any scheduling decisions."""

    def __init__(self, drivers: Iterable[HarnessDriver] = ()) -> None:
        self._drivers: dict[str, HarnessDriver] = {}
        for driver in drivers:
            self.register(driver)

    def register(self, driver: HarnessDriver, *, replace: bool = False) -> None:
        name = driver.capabilities().name.strip()
        if not name:
            raise ValueError("A harness driver must declare a non-empty name")
        if name in self._drivers and not replace:
            raise DriverAlreadyRegisteredError(
                f"Harness driver {name!r} is already registered"
            )
        self._drivers[name] = driver

    def get(self, name: str) -> HarnessDriver:
        try:
            return self._drivers[name]
        except KeyError as error:
            raise UnknownDriverError(
                f"Harness driver {name!r} is not registered"
            ) from error

    def remove(self, name: str) -> HarnessDriver:
        try:
            return self._drivers.pop(name)
        except KeyError as error:
            raise UnknownDriverError(
                f"Harness driver {name!r} is not registered"
            ) from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._drivers))

    def capabilities(self) -> tuple[HarnessCapabilities, ...]:
        return tuple(self._drivers[name].capabilities() for name in self.names())

    def __contains__(self, name: object) -> bool:
        return name in self._drivers

    def __len__(self) -> int:
        return len(self._drivers)
