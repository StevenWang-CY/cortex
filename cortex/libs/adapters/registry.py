"""
Cortex Adapter Registry

Central registry for managing pluggable workspace adapters.
Replaces the ad-hoc dict approach in InterventionExecutor.
"""

from __future__ import annotations

import logging
from typing import Any

from cortex.libs.adapters.base import AdapterResult, CortexAdapter

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """
    Registry for workspace adapters.

    Supports registration, discovery, and capability querying.
    Can discover adapters from Python entry points for plugin support.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, CortexAdapter] = {}

    def register(self, adapter: CortexAdapter) -> None:
        """Register an adapter. Overwrites if name already registered."""
        name = adapter.name
        self._adapters[name] = adapter
        logger.info("Registered adapter: %s (capabilities: %s)", name, adapter.capabilities)

    def register_legacy(self, name: str, adapter: Any) -> None:
        """Register a legacy WorkspaceAdapter that doesn't implement CortexAdapter.

        Wraps it in a LegacyAdapterWrapper for backward compatibility.
        """
        wrapped = _LegacyAdapterWrapper(name, adapter)
        self._adapters[name] = wrapped
        logger.info("Registered legacy adapter: %s", name)

    def get(self, name: str) -> CortexAdapter | None:
        """Get an adapter by name."""
        return self._adapters.get(name)

    def has(self, name: str) -> bool:
        """Check if an adapter is registered."""
        return name in self._adapters

    def list_adapters(self) -> list[str]:
        """List all registered adapter names."""
        return list(self._adapters.keys())

    def list_capabilities(self) -> dict[str, list[str]]:
        """Get capabilities for all registered adapters."""
        return {name: adapter.capabilities for name, adapter in self._adapters.items()}

    def find_adapter_for_action(self, action_type: str) -> CortexAdapter | None:
        """Find the first adapter that supports a given action type."""
        for adapter in self._adapters.values():
            if action_type in adapter.capabilities:
                return adapter
        return None

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks on all adapters."""
        results: dict[str, bool] = {}
        for name, adapter in self._adapters.items():
            try:
                results[name] = await adapter.health_check()
            except Exception:
                logger.exception("Health check failed for adapter '%s'", name)
                results[name] = False
        return results

    def discover_plugins(self) -> int:
        """
        Discover and register adapters from Python entry points.

        Entry point group: 'cortex.adapters'
        Each entry point should resolve to a class implementing CortexAdapter.

        Returns:
            Number of adapters discovered and registered.
        """
        count = 0
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group="cortex.adapters")
            for ep in eps:
                try:
                    adapter_cls = ep.load()
                    adapter = adapter_cls()
                    self.register(adapter)
                    count += 1
                except Exception:
                    logger.exception("Failed to load adapter plugin: %s", ep.name)
        except Exception:
            logger.debug("No cortex.adapters entry points found")
        return count

    def clear(self) -> None:
        """Remove all registered adapters."""
        self._adapters.clear()


class _LegacyAdapterWrapper:
    """Wraps a legacy WorkspaceAdapter to conform to CortexAdapter protocol."""

    def __init__(self, adapter_name: str, legacy_adapter: Any) -> None:
        self._name = adapter_name
        self._legacy = legacy_adapter

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> list[str]:
        return ["execute"]

    async def execute(self, action: str, params: dict[str, Any]) -> AdapterResult:
        try:
            success = await self._legacy.execute(action, params)
            return AdapterResult(success=success)
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def get_context(self) -> dict[str, Any]:
        return {}

    async def health_check(self) -> bool:
        return True
