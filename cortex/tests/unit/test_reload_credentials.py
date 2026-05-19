"""Audit-2 — ``AnthropicPlanner.reload_credentials`` BYOK hot-reload.

The planner caches its SDK client on construction. After the user saves
a fresh Bedrock token via onboarding / settings, ``reload_credentials``
must rebuild the SDK so the very next intervention uses the new key.
Without this the first session silently falls through to rule-based
fallback even though the user supplied a working token.

Tests:
1. Returns True when a fresh token is available in the keychain.
2. Returns False when no token is available.
3. Reloads succeed for vertex / direct providers (env-driven creds).
4. The cache is cleared on reload (stale plans don't survive).
5. ``daemon.reload_llm_credentials`` proxies to the planner correctly.
"""

from __future__ import annotations

import asyncio

import pytest

from cortex.libs.config.settings import LLMConfig
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner


class _FakeSDK:
    """Minimal stand-in for ``AsyncAnthropic*`` SDK clients. Just needs
    to exist as ``self._sdk``; reload_credentials replaces it."""

    def __init__(self, marker: str = "v1") -> None:
        self.marker = marker
        self.messages = self


def _make_planner(sdk_marker: str = "v1") -> AnthropicPlanner:
    cfg = LLMConfig()
    cfg.provider = "bedrock"
    planner = AnthropicPlanner(config=cfg, sdk=_FakeSDK(sdk_marker))
    return planner


def test_reload_with_keychain_token_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _make_planner("v1")
    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(
        ap, "_keychain_get_bedrock_token", lambda _cfg: "new-token-value"
    )
    monkeypatch.setattr(
        ap, "build_anthropic_sdk_client", lambda **_kw: _FakeSDK("v2"),
    )

    assert planner.reload_credentials() is True
    assert planner._sdk.marker == "v2"


def test_reload_without_keychain_token_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _make_planner("v1")
    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(ap, "_keychain_get_bedrock_token", lambda _cfg: None)
    monkeypatch.setattr(
        ap, "build_anthropic_sdk_client", lambda **_kw: _FakeSDK("never"),
    )

    assert planner.reload_credentials() is False
    # SDK NOT replaced on failure.
    assert planner._sdk.marker == "v1"


def test_reload_for_vertex_provider_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = LLMConfig()
    cfg.provider = "vertex"
    planner = AnthropicPlanner(config=cfg, sdk=_FakeSDK("v1"))
    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(
        ap, "build_anthropic_sdk_client", lambda **_kw: _FakeSDK("vertex-v2"),
    )
    assert planner.reload_credentials() is True
    assert planner._sdk.marker == "vertex-v2"


def test_reload_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reload_credentials`` must call ``LLMCache.clear`` so a stale
    plan cached against the old token never serves the user post-rotation."""
    planner = _make_planner("v1")
    # Replace the cache with a stub we can introspect — the real
    # cache's ``put`` requires a TaskContext + InterventionPlan which is
    # heavyweight; we only need to confirm ``clear`` is called.
    cleared = {"count": 0}

    class _StubCache:
        def clear(self) -> None:
            cleared["count"] += 1

    planner._cache = _StubCache()  # type: ignore[assignment]

    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(
        ap, "_keychain_get_bedrock_token", lambda _cfg: "new-token"
    )
    monkeypatch.setattr(
        ap, "build_anthropic_sdk_client", lambda **_kw: _FakeSDK("v2"),
    )

    assert planner.reload_credentials() is True
    assert cleared["count"] == 1


def test_daemon_reload_llm_credentials_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``daemon.reload_llm_credentials`` must call ``planner.reload_credentials``
    and return its result."""
    from cortex.services.runtime_daemon import CortexDaemon

    d = CortexDaemon.__new__(CortexDaemon)

    class _StubPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def reload_credentials(self) -> bool:
            self.calls += 1
            return True

    planner = _StubPlanner()
    d._llm_client = planner
    result = asyncio.run(d.reload_llm_credentials())
    assert result is True
    assert planner.calls == 1
