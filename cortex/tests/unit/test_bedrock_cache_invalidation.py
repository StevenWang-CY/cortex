"""P1-17: clear_bedrock_token_cache is public and forces a fresh keychain read."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def _reset_module_state() -> None:
    """Clear the module-level cache and lru_cache between subtests."""
    import cortex.libs.config.settings as s

    s._bedrock_token_cache = None
    s.get_config.cache_clear()


def test_public_name_is_exported() -> None:
    """clear_bedrock_token_cache (no leading underscore) must exist."""
    from cortex.libs.config.settings import clear_bedrock_token_cache

    assert callable(clear_bedrock_token_cache)


def test_private_alias_still_works() -> None:
    """The _clear_bedrock_token_cache alias must remain for backward compat."""
    from cortex.libs.config import settings

    assert settings._clear_bedrock_token_cache is settings.clear_bedrock_token_cache


def test_cache_populated_then_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the cache, call clear, verify next call reads keychain afresh."""
    import cortex.libs.config.settings as s

    _reset_module_state()

    # Ensure no env override interferes.
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    # First fake keychain call returns "token_v1".
    call_count = {"n": 0}

    def _fake_get_password(service: str, account: str) -> str | None:
        call_count["n"] += 1
        return "token_v1" if call_count["n"] == 1 else "token_v2"

    with patch("cortex.libs.utils.secrets.get_password_safe", side_effect=_fake_get_password):
        # Force a config that uses keychain so get_bedrock_token actually calls it.
        from cortex.libs.config.settings import BedrockConfig, CortexConfig, LLMConfig

        fake_cfg = CortexConfig(
            llm=LLMConfig(provider="bedrock", use_keychain=True, bedrock=BedrockConfig())
        )

        token1 = s.get_bedrock_token(config=fake_cfg)
        assert token1 == "token_v1"
        assert call_count["n"] == 1

        # Cache is populated; second call should NOT hit keychain again.
        token1_cached = s.get_bedrock_token(config=fake_cfg)
        assert token1_cached == "token_v1"
        assert call_count["n"] == 1, "Cache should have served the second call"

        # Clear the cache — next call must read keychain afresh.
        s.clear_bedrock_token_cache()

        token2 = s.get_bedrock_token(config=fake_cfg)
        assert token2 == "token_v2"
        assert call_count["n"] == 2, "Expected a fresh keychain read after cache clear"

    _reset_module_state()
