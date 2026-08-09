"""Harness adapter interfaces and built-in implementations."""

from agentd.harness.codex import CodexCliDriver, CodexDriver
from agentd.harness.codex_sdk import CodexSdkDriver
from agentd.harness.errors import (
    DriverAlreadyRegisteredError,
    HarnessError,
    RunNotActiveError,
    UnknownDriverError,
    UnknownRunError,
)
from agentd.harness.fake import FakeHarnessCall, FakeHarnessDriver
from agentd.harness.protocol import HarnessDriver, ManagedHarnessDriver
from agentd.harness.registry import DriverRegistry

__all__ = [
    "CodexCliDriver",
    "CodexDriver",
    "CodexSdkDriver",
    "DriverAlreadyRegisteredError",
    "DriverRegistry",
    "FakeHarnessCall",
    "FakeHarnessDriver",
    "HarnessDriver",
    "HarnessError",
    "ManagedHarnessDriver",
    "RunNotActiveError",
    "UnknownDriverError",
    "UnknownRunError",
]
