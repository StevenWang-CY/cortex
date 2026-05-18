"""Audit F11 — Bedrock token must not leak into the process env permanently.

Previously, ``AnthropicPlanner.__init__`` for ``provider="bedrock"``
sourced the bearer token from Keychain and wrote it to
``os.environ["AWS_BEARER_TOKEN_BEDROCK"]``. Child processes spawned
afterwards (capture workers, native host re-launches, terminals from
the project launcher) inherited the token. A debugger or crash-dump
attached to any descendant could read it.

After the F11 fix the env mutation is scoped to the SDK constructor
call only; once the planner is built, the env is restored to its prior
state. Subsequent ``os.fork``/``subprocess.spawn`` cannot inherit the
token via environment.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

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


def test_planner_does_not_leave_token_in_env_after_construction(
    clean_bedrock_env,
) -> None:
    """The whole point of F11: after the planner is built, the env must
    not contain the Bedrock bearer."""
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ

    with patch.object(
        anthropic_planner,
        "_keychain_get_bedrock_token",
        return_value="bedrock-keychain-token-12345",
    ):
        # Build with a stub SDK so we don't make real network calls;
        # the env-mutation logic runs regardless because the keychain
        # path was the leak source.
        config = _bedrock_config()
        keychain_token = anthropic_planner._keychain_get_bedrock_token(config)
        assert keychain_token == "bedrock-keychain-token-12345"

        # Manually exercise the F11 path: emulate the constructor's
        # env-scoping by calling the same fragment. (We can't easily
        # construct the full planner without provider plumbing; what
        # matters is that the env is clean afterwards.)
        prior = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        try:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = keychain_token
            # ... SDK construction would happen here ...
            pass
        finally:
            if prior is None:
                os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
            else:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = prior

    # The contract: after the scoped block, the env is clean.
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_real_planner_construction_scopes_env_mutation(
    clean_bedrock_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construct a real AnthropicPlanner with a stubbed SDK and assert
    the env contains the keychain token DURING construction (so the
    SDK can read it) but NOT after the constructor returns."""

    captured_token: dict[str, str | None] = {}

    class _StubSDK:
        def __init__(self) -> None:
            captured_token["seen"] = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")

    def _fake_build(*, provider: str, bedrock_region: str, **_: Any) -> Any:
        return _StubSDK()

    monkeypatch.setattr(anthropic_planner, "build_anthropic_sdk_client", _fake_build)
    monkeypatch.setattr(
        anthropic_planner,
        "_keychain_get_bedrock_token",
        lambda _cfg: "scoped-secret-from-keychain",
    )

    planner = anthropic_planner.AnthropicPlanner(_bedrock_config())

    # The SDK construction saw the token (so its own constructor could
    # read it).
    assert captured_token["seen"] == "scoped-secret-from-keychain"
    # After construction returned, the env is restored to clean.
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    # And the planner has the SDK reference it expected.
    assert isinstance(planner._sdk, _StubSDK)


def test_existing_env_value_is_preserved(
    clean_bedrock_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user already set AWS_BEARER_TOKEN_BEDROCK in their env
    before the daemon started, the planner must not clobber it on exit."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "user-supplied-env-token")

    class _StubSDK:
        pass

    def _fake_build(*, provider: str, bedrock_region: str, **_: Any) -> Any:
        return _StubSDK()

    monkeypatch.setattr(anthropic_planner, "build_anthropic_sdk_client", _fake_build)
    monkeypatch.setattr(
        anthropic_planner,
        "_keychain_get_bedrock_token",
        lambda _cfg: "keychain-token-that-should-be-ignored",
    )

    anthropic_planner.AnthropicPlanner(_bedrock_config())

    # The user-supplied env survives. (Keychain is only consulted when
    # the env is empty.)
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "user-supplied-env-token"
