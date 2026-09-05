"""Request shaping + provider/model resolution for the Anthropic transport.

Covers the audit findings around the request itself:

* D1 — no sampling parameters (``temperature`` returns HTTP 400 on current
  models); no ``tools`` / forced ``tool_choice`` (structured outputs only).
* D2 — provider ids are the Bedrock Mantle ``anthropic.*`` ids / Vertex ids,
  and the Bedrock bearer token is passed explicitly to the SDK constructor.
* ``output_config.effort`` is sent only to models that accept it.
* Every SDK client is built with ``max_retries=0``.
"""

from __future__ import annotations

import os
from typing import get_args

import pytest
from anthropic import AsyncAnthropic, AsyncAnthropicBedrockMantle, AsyncAnthropicVertex

from cortex.libs.config.settings import LLMConfig, LogicalModelId
from cortex.libs.llm.anthropic_client import (
    _BEDROCK_MODEL_IDS,
    _VERTEX_MODEL_IDS,
    MODEL_CAPABILITIES,
    EffortLevel,
    build_anthropic_sdk_client,
    model_capabilities,
    model_capabilities_or_conservative,
    resolve_anthropic_model_id,
)
from cortex.libs.llm.pricing import PRICES_USD_PER_MTOK
from cortex.services.llm_engine.anthropic_planner import build_request_kwargs
from cortex.services.llm_engine.plan_draft import structured_output_schema

_LOGICAL: tuple[str, ...] = tuple(get_args(LogicalModelId))

_SYSTEM = [{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}]
_MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


# ---------------------------------------------------------------------------
# Tables are keyed by the single canonical literal
# ---------------------------------------------------------------------------


def test_every_table_covers_exactly_the_logical_model_literal() -> None:
    expected = set(_LOGICAL)
    assert expected == {
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    }
    assert set(MODEL_CAPABILITIES) == expected
    assert set(_BEDROCK_MODEL_IDS) == expected
    assert set(_VERTEX_MODEL_IDS) == expected
    assert set(PRICES_USD_PER_MTOK) == expected


def test_config_defaults_use_current_generation_models() -> None:
    cfg = LLMConfig()
    assert cfg.model_default == "claude-sonnet-5"
    assert cfg.model_fast == "claude-haiku-4-5"
    assert cfg.model_deep == "claude-opus-5"
    assert cfg.max_tokens == 8192
    assert cfg.effort == "medium"
    assert "temperature" not in LLMConfig.model_fields


def test_effort_literal_matches_config_field() -> None:
    assert set(get_args(EffortLevel)) == set(get_args(LLMConfig.model_fields["effort"].annotation))
    assert set(get_args(EffortLevel)) == {"low", "medium", "high", "xhigh", "max"}


def test_max_tokens_floor_is_enforced() -> None:
    with pytest.raises(ValueError):
        LLMConfig(max_tokens=512)


# ---------------------------------------------------------------------------
# Provider id resolution: 3 providers × 5 models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logical", "provider", "expected"),
    [
        ("claude-opus-5", "bedrock", "anthropic.claude-opus-5"),
        ("claude-sonnet-5", "bedrock", "anthropic.claude-sonnet-5"),
        ("claude-haiku-4-5", "bedrock", "anthropic.claude-haiku-4-5"),
        ("claude-opus-4-7", "bedrock", "anthropic.claude-opus-4-7"),
        ("claude-sonnet-4-6", "bedrock", "anthropic.claude-sonnet-4-6"),
        ("claude-opus-5", "vertex", "claude-opus-5"),
        ("claude-sonnet-5", "vertex", "claude-sonnet-5"),
        ("claude-haiku-4-5", "vertex", "claude-haiku-4-5@20251001"),
        ("claude-opus-4-7", "vertex", "claude-opus-4-7"),
        ("claude-sonnet-4-6", "vertex", "claude-sonnet-4-6"),
        ("claude-opus-5", "direct", "claude-opus-5"),
        ("claude-sonnet-5", "direct", "claude-sonnet-5"),
        ("claude-haiku-4-5", "direct", "claude-haiku-4-5"),
        ("claude-opus-4-7", "direct", "claude-opus-4-7"),
        ("claude-sonnet-4-6", "direct", "claude-sonnet-4-6"),
    ],
)
def test_provider_model_id_resolution(logical: str, provider: str, expected: str) -> None:
    assert resolve_anthropic_model_id(logical, provider=provider) == expected  # type: ignore[arg-type]


def test_no_legacy_inference_profile_ids_remain() -> None:
    for provider in ("bedrock", "vertex", "direct"):
        for logical in _LOGICAL:
            resolved = resolve_anthropic_model_id(logical, provider=provider)  # type: ignore[arg-type]
            assert "us.anthropic." not in resolved
            assert not resolved.endswith("-v1:0")


def test_unknown_provider_and_model_fail_loudly() -> None:
    with pytest.raises(ValueError):
        resolve_anthropic_model_id("claude-sonnet-5", provider="azure")
    with pytest.raises(KeyError):
        resolve_anthropic_model_id("claude-3-opus", provider="bedrock")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        resolve_anthropic_model_id("claude-3-opus", provider="direct")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Capability table
# ---------------------------------------------------------------------------


def test_capability_rows_match_the_api_reference() -> None:
    no_sampling = {"claude-opus-5", "claude-sonnet-5", "claude-opus-4-7"}
    for logical in _LOGICAL:
        caps = MODEL_CAPABILITIES[logical]  # type: ignore[index]
        assert caps.supports_sampling_params == (logical not in no_sampling)
        assert caps.supports_effort == (logical != "claude-haiku-4-5")
        assert caps.thinking_on_by_default == (logical in {"claude-opus-5", "claude-sonnet-5"})
    prefixes = {
        "claude-opus-5": 512,
        "claude-sonnet-5": 1024,
        "claude-sonnet-4-6": 1024,
        "claude-opus-4-7": 2048,
        "claude-haiku-4-5": 4096,
    }
    for logical, prefix in prefixes.items():
        assert MODEL_CAPABILITIES[logical].min_cache_prefix_tokens == prefix  # type: ignore[index]


def test_capabilities_accept_provider_ids_and_degrade_for_unknown() -> None:
    assert model_capabilities("anthropic.claude-haiku-4-5").supports_effort is False
    assert model_capabilities("claude-haiku-4-5@20251001").supports_effort is False
    assert model_capabilities("us.anthropic.claude-opus-4-7-v1:0").supports_effort is True
    with pytest.raises(KeyError):
        model_capabilities("claude-unknown-9")
    conservative = model_capabilities_or_conservative("claude-unknown-9")
    assert conservative.supports_effort is False
    assert conservative.supports_sampling_params is False


# ---------------------------------------------------------------------------
# Request kwargs per model family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "anthropic.claude-sonnet-5",
        "anthropic.claude-opus-4-7",
        "claude-opus-5",
    ],
)
def test_effort_is_sent_to_models_that_support_it(model_id: str) -> None:
    kwargs = build_request_kwargs(
        model_id=model_id,
        max_tokens=8192,
        effort="medium",
        system_blocks=_SYSTEM,
        messages=_MESSAGES,
        timeout_seconds=30.0,
    )
    assert kwargs["output_config"]["effort"] == "medium"


@pytest.mark.parametrize(
    "model_id",
    ["claude-haiku-4-5", "anthropic.claude-haiku-4-5", "claude-haiku-4-5@20251001", "claude-x-99"],
)
def test_effort_is_omitted_for_haiku_and_unknown_models(model_id: str) -> None:
    kwargs = build_request_kwargs(
        model_id=model_id,
        max_tokens=8192,
        effort="high",
        system_blocks=_SYSTEM,
        messages=_MESSAGES,
        timeout_seconds=30.0,
    )
    assert "effort" not in kwargs["output_config"]


def test_effort_none_is_never_sent() -> None:
    kwargs = build_request_kwargs(
        model_id="claude-sonnet-5",
        max_tokens=8192,
        effort=None,
        system_blocks=_SYSTEM,
        messages=_MESSAGES,
        timeout_seconds=30.0,
    )
    assert "effort" not in kwargs["output_config"]


@pytest.mark.parametrize("logical", _LOGICAL)
def test_request_never_carries_sampling_params_tools_or_thinking(logical: str) -> None:
    for provider in ("bedrock", "vertex", "direct"):
        model_id = resolve_anthropic_model_id(logical, provider=provider)  # type: ignore[arg-type]
        kwargs = build_request_kwargs(
            model_id=model_id,
            max_tokens=8192,
            effort="medium",
            system_blocks=_SYSTEM,
            messages=_MESSAGES,
            timeout_seconds=30.0,
        )
        assert set(kwargs) == {
            "model",
            "max_tokens",
            "system",
            "messages",
            "output_config",
            "timeout",
        }
        for forbidden in ("temperature", "top_p", "top_k", "tools", "tool_choice", "thinking"):
            assert forbidden not in kwargs
        assert kwargs["model"] == model_id
        assert kwargs["max_tokens"] == 8192
        assert kwargs["timeout"] == 30.0
        assert kwargs["system"] is _SYSTEM
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        fmt = kwargs["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["additionalProperties"] is False
        assert "situation_summary" in fmt["schema"]["properties"]


def test_request_uses_supplied_schema_or_builds_the_draft_schema() -> None:
    custom = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    kwargs = build_request_kwargs(
        model_id="claude-sonnet-5",
        max_tokens=2048,
        effort="low",
        system_blocks=_SYSTEM,
        messages=_MESSAGES,
        timeout_seconds=5.0,
        output_schema=custom,
    )
    assert kwargs["output_config"]["format"]["schema"] is custom
    default = build_request_kwargs(
        model_id="claude-sonnet-5",
        max_tokens=2048,
        effort="low",
        system_blocks=_SYSTEM,
        messages=_MESSAGES,
        timeout_seconds=5.0,
    )
    assert default["output_config"]["format"]["schema"] == structured_output_schema()


# ---------------------------------------------------------------------------
# SDK client construction: explicit credentials, max_retries=0
# ---------------------------------------------------------------------------


def test_bedrock_client_takes_the_token_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    client = build_anthropic_sdk_client(
        provider="bedrock",
        bedrock_region="us-east-2",
        bedrock_bearer_token="explicit-token",
    )
    assert isinstance(client, AsyncAnthropicBedrockMantle)
    assert client.api_key == "explicit-token"
    assert client.aws_region == "us-east-2"
    assert client.max_retries == 0
    # The bearer is sent as Authorization: Bearer, never as x-api-key.
    assert client.auth_headers == {"Authorization": "Bearer explicit-token"}
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_bedrock_client_falls_back_to_env_then_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "env-token")
    client = build_anthropic_sdk_client(provider="bedrock", bedrock_region="us-east-2")
    assert isinstance(client, AsyncAnthropicBedrockMantle)
    assert client.api_key == "env-token"
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    with pytest.raises(RuntimeError):
        build_anthropic_sdk_client(provider="bedrock", bedrock_region="us-east-2")


def test_direct_client_requires_key_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        build_anthropic_sdk_client(provider="direct")
    client = build_anthropic_sdk_client(provider="direct", anthropic_api_key="sk-ant-test")
    assert isinstance(client, AsyncAnthropic)
    assert client.max_retries == 0
    assert client.api_key == "sk-ant-test"


def test_vertex_client_uses_region_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    client = build_anthropic_sdk_client(provider="vertex")
    assert isinstance(client, AsyncAnthropicVertex)
    assert client.region == "us-east5"
    assert client.max_retries == 0
    explicit = build_anthropic_sdk_client(
        provider="vertex", vertex_region="europe-west1", vertex_project_id="proj-1"
    )
    assert isinstance(explicit, AsyncAnthropicVertex)
    assert explicit.region == "europe-west1"
    assert explicit.project_id == "proj-1"


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        build_anthropic_sdk_client(provider="azure")
