"""Tests for the Adapter Registry."""

from __future__ import annotations

from typing import Any

import pytest

from cortex.libs.adapters.base import AdapterResult
from cortex.libs.adapters.registry import AdapterRegistry

# ---------------------------------------------------------------------------
# Stub adapter implementing the CortexAdapter protocol
# ---------------------------------------------------------------------------

class StubAdapter:
    """Minimal adapter for testing."""

    def __init__(self, adapter_name: str, caps: list[str] | None = None):
        self._name = adapter_name
        self._caps = caps or ["close_tab", "open_url"]

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> list[str]:
        return list(self._caps)

    async def execute(self, action: str, params: dict[str, Any]) -> AdapterResult:
        return AdapterResult(success=True, data={"action": action})

    async def get_context(self) -> dict[str, Any]:
        return {"tabs": 5}

    async def health_check(self) -> bool:
        return True


@pytest.fixture
def registry():
    return AdapterRegistry()


@pytest.fixture
def chrome_adapter():
    return StubAdapter("chrome", ["close_tab", "group_tabs", "open_url"])


@pytest.fixture
def vscode_adapter():
    return StubAdapter("vscode", ["fold_code", "highlight_tab"])


# ---------------------------------------------------------------------------
# Register and get adapter
# ---------------------------------------------------------------------------

class TestRegisterAndGet:
    def test_register_then_get(self, registry, chrome_adapter):
        registry.register(chrome_adapter)
        adapter = registry.get("chrome")
        assert adapter is not None
        assert adapter.name == "chrome"

    def test_has_returns_true_after_register(self, registry, chrome_adapter):
        registry.register(chrome_adapter)
        assert registry.has("chrome") is True

    def test_list_adapters(self, registry, chrome_adapter, vscode_adapter):
        registry.register(chrome_adapter)
        registry.register(vscode_adapter)
        names = registry.list_adapters()
        assert "chrome" in names
        assert "vscode" in names

    def test_overwrite_on_re_register(self, registry):
        adapter1 = StubAdapter("browser", ["close_tab"])
        adapter2 = StubAdapter("browser", ["open_url"])
        registry.register(adapter1)
        registry.register(adapter2)
        got = registry.get("browser")
        assert got is adapter2

    def test_find_adapter_for_action(self, registry, chrome_adapter, vscode_adapter):
        registry.register(chrome_adapter)
        registry.register(vscode_adapter)
        adapter = registry.find_adapter_for_action("fold_code")
        assert adapter is not None
        assert adapter.name == "vscode"

    def test_clear(self, registry, chrome_adapter):
        registry.register(chrome_adapter)
        registry.clear()
        assert registry.list_adapters() == []


# ---------------------------------------------------------------------------
# list_capabilities
# ---------------------------------------------------------------------------

class TestListCapabilities:
    def test_capabilities_structure(self, registry, chrome_adapter, vscode_adapter):
        registry.register(chrome_adapter)
        registry.register(vscode_adapter)
        caps = registry.list_capabilities()
        assert isinstance(caps, dict)
        assert "chrome" in caps
        assert "close_tab" in caps["chrome"]
        assert "fold_code" in caps["vscode"]

    def test_empty_registry_capabilities(self, registry):
        caps = registry.list_capabilities()
        assert caps == {}


# ---------------------------------------------------------------------------
# Missing adapter returns None
# ---------------------------------------------------------------------------

class TestMissingAdapter:
    def test_get_missing_returns_none(self, registry):
        assert registry.get("nonexistent") is None

    def test_has_missing_returns_false(self, registry):
        assert registry.has("nonexistent") is False

    def test_find_action_missing_returns_none(self, registry):
        assert registry.find_adapter_for_action("unknown_action") is None
