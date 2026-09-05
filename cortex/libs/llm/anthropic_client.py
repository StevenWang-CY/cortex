"""Anthropic SDK transport factory, model-capability table, and model resolver.

Every Cortex service that needs Claude goes through ``LLMClient`` (see
``cortex.services.llm_engine.client``), which in turn is constructed by
:func:`build_anthropic_sdk_client`. The provider is selected by
``LLMConfig.provider`` (or the ``ANTHROPIC_PROVIDER`` environment variable
when no explicit value is passed):

- ``bedrock`` (default): ``AsyncAnthropicBedrockMantle`` — the Messages-API
  Bedrock endpoint. Authenticates with a long-lived bearer token passed
  **explicitly** as ``api_key`` (the caller sources it from the macOS
  Keychain, BYOK); ``AWS_BEARER_TOKEN_BEDROCK`` is only an env fallback.
  The SDK sends it as ``Authorization: Bearer``.
- ``vertex``: ``AsyncAnthropicVertex`` — Google Cloud residency failover
  (Application Default Credentials).
- ``direct``: ``AsyncAnthropic`` — the first-party API with
  ``ANTHROPIC_API_KEY``.

Every client is built with ``max_retries=0``: the planner owns the only
retry policy, so its worst-case latency (``LLMConfig.planner_worst_case_seconds``)
is exact rather than multiplied by hidden SDK retries.

The three tables below (capabilities, Bedrock ids, Vertex ids) are keyed by
the canonical :data:`LogicalModel` literal so a tier added in ``settings.py``
is immediately visible to every consumer; ``test_llm_request_kwargs.py``
asserts the key sets stay identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from anthropic import AsyncAnthropic, AsyncAnthropicBedrockMantle, AsyncAnthropicVertex

# Single-source-of-truth: the canonical logical-model literal lives in
# :mod:`cortex.libs.config.settings` as ``LogicalModelId``. This module
# re-exports it under the historical name ``LogicalModel`` so the two
# definitions can never drift. ``cortex.libs.config.settings`` does not
# import this module, so there is no import cycle.
from cortex.libs.config.settings import LogicalModelId as LogicalModel
from cortex.libs.llm.pricing import normalize_model_id

Provider = Literal["bedrock", "vertex", "direct"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
AnthropicAsyncClient = AsyncAnthropic | AsyncAnthropicBedrockMantle | AsyncAnthropicVertex


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Request-shaping facts the planner needs per logical model.

    ``supports_sampling_params``: whether ``temperature`` / ``top_p`` /
    ``top_k`` are accepted. Opus 4.7 / Opus 5 / Sonnet 5 return HTTP 400
    when any is present; Cortex never sends them for any model.
    ``supports_effort``: whether ``output_config.effort`` is accepted
    (errors on Haiku 4.5).
    ``thinking_on_by_default``: adaptive thinking runs when ``thinking`` is
    omitted, so ``max_tokens`` caps thinking **plus** text.
    ``min_cache_prefix_tokens``: shortest prefix the prompt cache will
    store; shorter system prompts silently never cache.
    """

    supports_sampling_params: bool
    supports_effort: bool
    thinking_on_by_default: bool
    min_cache_prefix_tokens: int


MODEL_CAPABILITIES: dict[LogicalModel, ModelCapabilities] = {
    "claude-opus-5": ModelCapabilities(
        supports_sampling_params=False,
        supports_effort=True,
        thinking_on_by_default=True,
        min_cache_prefix_tokens=512,
    ),
    "claude-sonnet-5": ModelCapabilities(
        supports_sampling_params=False,
        supports_effort=True,
        thinking_on_by_default=True,
        min_cache_prefix_tokens=1024,
    ),
    "claude-haiku-4-5": ModelCapabilities(
        supports_sampling_params=True,
        supports_effort=False,
        thinking_on_by_default=False,
        min_cache_prefix_tokens=4096,
    ),
    "claude-opus-4-7": ModelCapabilities(
        supports_sampling_params=False,
        supports_effort=True,
        thinking_on_by_default=False,
        min_cache_prefix_tokens=2048,
    ),
    "claude-sonnet-4-6": ModelCapabilities(
        supports_sampling_params=True,
        supports_effort=True,
        thinking_on_by_default=False,
        min_cache_prefix_tokens=1024,
    ),
}

# Conservative shape used when a model id is unknown to the tables (e.g. a
# future tier reached through ``template_tier_overrides`` before the
# tables are updated): send nothing optional.
_CONSERVATIVE_CAPABILITIES = ModelCapabilities(
    supports_sampling_params=False,
    supports_effort=False,
    thinking_on_by_default=True,
    min_cache_prefix_tokens=4096,
)

# Bedrock Mantle (Messages API) model ids. Region routing is handled by the
# Mantle endpoint; no ``us.``/``-v1:0`` inference-profile decoration.
_BEDROCK_MODEL_IDS: dict[LogicalModel, str] = {
    "claude-opus-5": "anthropic.claude-opus-5",
    "claude-sonnet-5": "anthropic.claude-sonnet-5",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5",
    "claude-opus-4-7": "anthropic.claude-opus-4-7",
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
}

# Vertex AI model identifiers. Current-generation models use the bare
# first-party id; Haiku 4.5 is a dated snapshot with an ``@`` separator.
_VERTEX_MODEL_IDS: dict[LogicalModel, str] = {
    "claude-opus-5": "claude-opus-5",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-haiku-4-5": "claude-haiku-4-5@20251001",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
}


def _resolve_provider(provider: str | None) -> Provider:
    raw = (provider or os.getenv("ANTHROPIC_PROVIDER") or "bedrock").lower()
    if raw not in {"bedrock", "vertex", "direct"}:
        raise ValueError(
            f"Unknown ANTHROPIC_PROVIDER={raw!r}; expected bedrock|vertex|direct",
        )
    return cast(Provider, raw)


def resolve_anthropic_model_id(
    logical: LogicalModel,
    provider: str | None = None,
) -> str:
    """Map a Cortex logical model ID to the provider-specific identifier.

    Args:
        logical: One of the :data:`LogicalModel` literal members.
        provider: Override for the ``ANTHROPIC_PROVIDER`` env var.

    Returns:
        For Bedrock: the Mantle model id (e.g. ``anthropic.claude-sonnet-5``).
        For Vertex: the Vertex id (bare, or ``@date`` for Haiku 4.5).
        For direct: the canonical model name unchanged.

    Raises:
        KeyError: if the logical ID is not a known Cortex model tier.
        ValueError: if the provider is unrecognised.
    """
    p = _resolve_provider(provider)
    if p == "bedrock":
        return _BEDROCK_MODEL_IDS[logical]
    if p == "vertex":
        return _VERTEX_MODEL_IDS[logical]
    if logical not in MODEL_CAPABILITIES:
        raise KeyError(logical)
    return logical


def model_capabilities(model_id: str) -> ModelCapabilities:
    """Return the capability row for a logical **or** provider model id.

    Raises:
        KeyError: when the id does not normalise to a known tier.
    """
    logical = cast(LogicalModel, normalize_model_id(model_id))
    return MODEL_CAPABILITIES[logical]


def model_capabilities_or_conservative(model_id: str) -> ModelCapabilities:
    """Like :func:`model_capabilities` but never raises.

    Unknown ids get the conservative row: no sampling params, no
    ``effort``, thinking assumed on. The planner uses this so an override
    to an unlisted model degrades to a plain request instead of a crash.
    """
    try:
        return model_capabilities(model_id)
    except KeyError:
        return _CONSERVATIVE_CAPABILITIES


def build_anthropic_sdk_client(
    *,
    provider: str | None = None,
    bedrock_region: str | None = None,
    bedrock_bearer_token: str | None = None,
    anthropic_api_key: str | None = None,
    vertex_region: str | None = None,
    vertex_project_id: str | None = None,
) -> AnthropicAsyncClient:
    """Construct the right Anthropic async client for the current provider.

    The resulting object exposes the unified ``messages.create(...)`` API,
    so callers (the Cortex planner) are provider-agnostic. Credentials are
    passed to the SDK constructor explicitly — this function never mutates
    ``os.environ``.

    Args:
        provider: Override for the ``ANTHROPIC_PROVIDER`` env var.
        bedrock_region: AWS region; defaults to ``AWS_REGION`` env or
            ``us-east-2``.
        bedrock_bearer_token: Long-lived Bedrock bearer token, passed to
            ``AsyncAnthropicBedrockMantle(api_key=...)``; falls back to the
            ``AWS_BEARER_TOKEN_BEDROCK`` env var.
        anthropic_api_key: Direct Anthropic API key; defaults to
            ``ANTHROPIC_API_KEY`` env var.
        vertex_region: GCP region for Vertex; defaults to
            ``GOOGLE_CLOUD_REGION`` env or ``us-east5``.
        vertex_project_id: Optional GCP project id; when omitted the SDK
            resolves it from Application Default Credentials.

    Returns:
        The configured async client, always with ``max_retries=0``.

    Raises:
        RuntimeError: when credentials for the selected provider are absent.
        ValueError: when ``provider`` is unrecognised.
    """
    p = _resolve_provider(provider)
    if p == "bedrock":
        region = bedrock_region or os.getenv("AWS_REGION") or "us-east-2"
        token = bedrock_bearer_token or os.getenv("AWS_BEARER_TOKEN_BEDROCK")
        if not token:
            raise RuntimeError(
                "No Bedrock bearer token available; cannot build the Bedrock "
                "client. Run the BYOK step in onboarding (Keychain) or export "
                "AWS_BEARER_TOKEN_BEDROCK before launching the daemon.",
            )
        return AsyncAnthropicBedrockMantle(
            aws_region=region,
            api_key=token,
            max_retries=0,
        )

    if p == "vertex":
        region = vertex_region or os.getenv("GOOGLE_CLOUD_REGION") or "us-east5"
        vertex_kwargs: dict[str, Any] = {"region": region, "max_retries": 0}
        if vertex_project_id:
            vertex_kwargs["project_id"] = vertex_project_id
        return AsyncAnthropicVertex(**vertex_kwargs)

    # direct
    api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing; cannot build direct Anthropic client.",
        )
    return AsyncAnthropic(api_key=api_key, max_retries=0)


__all__ = [
    "MODEL_CAPABILITIES",
    "AnthropicAsyncClient",
    "EffortLevel",
    "LogicalModel",
    "ModelCapabilities",
    "Provider",
    "build_anthropic_sdk_client",
    "model_capabilities",
    "model_capabilities_or_conservative",
    "resolve_anthropic_model_id",
]
