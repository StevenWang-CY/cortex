"""Audit F11 / D2 — the Bedrock token never touches the process env.

Previously ``AnthropicPlanner.__init__`` for ``provider="bedrock"`` sourced
the bearer token from Keychain and wrote it to
``os.environ["AWS_BEARER_TOKEN_BEDROCK"]`` (first permanently, later inside
a scoped window) so the SDK could read it back. Child processes spawned
during that window inherited the token, and the window itself was a
race. The token is now passed explicitly to
``build_anthropic_sdk_client(bedrock_bearer_token=...)`` and on to
``AsyncAnthropicBedrockMantle(api_key=...)``; ``os.environ`` is never
mutated by any LLM module.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.services.llm_engine import anthropic_planner


@pytest.fixture
def clean_bedrock_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    yield


def _bedrock_config() -> LLMConfig:
    return LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
    )


def test_llm_modules_never_mutate_the_process_environment() -> None:
    """Static guard: no LLM module assigns to or pops ``os.environ``."""
    import inspect

    from cortex.libs.llm import anthropic_client
    from cortex.services.llm_engine import context_broker

    for module in (anthropic_planner, anthropic_client, context_broker):
        source = inspect.getsource(module)
        assert "os.environ[" not in source.replace("os.environ.get(", ""), module.__name__
        assert "os.environ.pop(" not in source, module.__name__
        assert "os.putenv(" not in source, module.__name__


def test_real_planner_construction_passes_token_explicitly(
    clean_bedrock_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construct a real AnthropicPlanner with a stubbed transport factory
    and assert the keychain token reaches it as an explicit argument while
    ``os.environ`` stays untouched throughout (audit D2)."""

    captured: dict[str, str | None] = {}

    class _StubSDK:
        pass

    def _fake_build(
        *,
        provider: str,
        bedrock_region: str,
        bedrock_bearer_token: str | None = None,
        **_: Any,
    ) -> Any:
        captured["provider"] = provider
        captured["region"] = bedrock_region
        captured["token"] = bedrock_bearer_token
        captured["env_during_build"] = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        return _StubSDK()

    monkeypatch.setattr(anthropic_planner, "build_anthropic_sdk_client", _fake_build)
    monkeypatch.setattr(
        anthropic_planner,
        "_keychain_get_bedrock_token",
        lambda _cfg: "scoped-secret-from-keychain",
    )

    planner = anthropic_planner.AnthropicPlanner(_bedrock_config())

    assert captured["token"] == "scoped-secret-from-keychain"
    assert captured["provider"] == "bedrock"
    assert captured["region"] == "us-east-2"
    # Never in the environment — not even transiently during construction.
    assert captured["env_during_build"] is None
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    assert isinstance(planner._sdk, _StubSDK)


def test_existing_env_value_is_preserved(
    clean_bedrock_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user already set AWS_BEARER_TOKEN_BEDROCK in their env
    before the daemon started, the planner must not clobber it on exit."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "user-supplied-env-token")

    captured: dict[str, str | None] = {}

    class _StubSDK:
        pass

    def _fake_build(
        *,
        provider: str,
        bedrock_region: str,
        bedrock_bearer_token: str | None = None,
        **_: Any,
    ) -> Any:
        captured["token"] = bedrock_bearer_token
        return _StubSDK()

    monkeypatch.setattr(anthropic_planner, "build_anthropic_sdk_client", _fake_build)
    monkeypatch.setattr(
        anthropic_planner,
        "_keychain_get_bedrock_token",
        lambda _cfg: "keychain-token-that-should-be-ignored",
    )

    anthropic_planner.AnthropicPlanner(_bedrock_config())

    # The user-supplied env survives and is what the SDK receives.
    # (Keychain is only consulted when the env is empty.)
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "user-supplied-env-token"
    assert captured["token"] == "user-supplied-env-token"
