"""Stateful runtime managers used by the coordinator."""

from agentd.runtime.quota import QuotaManager
from agentd.runtime.resources import ResourceManager

__all__ = ["QuotaManager", "ResourceManager"]
