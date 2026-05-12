"""Anthropic SDK transport factory + logical-to-provider model resolver.

Mirrors the Civitas pattern: every Cortex service that needs Claude
goes through ``LLMClient`` (see ``cortex.services.llm_engine.client``),
which in turn is constructed by ``build_anthropic_sdk_client``. The
provider is selected by the ``ANTHROPIC_PROVIDER`` environment variable:

- ``bedrock`` (default): ``AsyncAnthropicBedrock`` — production. Uses
  AWS inference profiles in ``us-east-2`` and the
  ``AWS_BEARER_TOKEN_BEDROCK`` long-lived bearer token, sourced from the
  macOS Keychain (BYOK) at daemon startup.
- ``vertex``: ``AsyncAnthropicVertex`` — Google Cloud region failover.
- ``direct``: ``AsyncAnthropic`` — direct Anthropic API for environments
  where the personal billing cap has lifted; uses ``ANTHROPIC_API_KEY``.

Bedrock inference-profile IDs change as Anthropic releases new revisions.
The table below is the source of truth — bump it when AWS publishes new
profile IDs. ``us.anthropic.*`` profiles are cross-region routed inside
the ``us`` cluster (us-east-1, us-east-2, us-west-2).
"""

from __future__ import annotations

import os
from typing import Literal, cast

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock, AsyncAnthropicVertex

LogicalModel = Literal[
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

Provider = Literal["bedrock", "vertex", "direct"]

# Bedrock cross-region inference profiles (us-east-2 origin).
# These IDs route to the closest healthy region in the us cluster.
_BEDROCK_INFERENCE_PROFILES: dict[LogicalModel, str] = {
    "claude-opus-4-7": "us.anthropic.claude-opus-4-7-v1:0",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6-v1:0",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-v1:0",
}

# Vertex AI model identifiers — equivalent revisions for residency failover.
_VERTEX_MODEL_IDS: dict[LogicalModel, str] = {
    "claude-opus-4-7": "claude-opus-4-7@20251101",
    "claude-sonnet-4-6": "claude-sonnet-4-6@20251015",
    "claude-haiku-4-5": "claude-haiku-4-5@20251001",
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
        logical: One of ``claude-opus-4-7``, ``claude-sonnet-4-6``,
            ``claude-haiku-4-5``.
        provider: Override for ``ANTHROPIC_PROVIDER`` env var.

    Returns:
        For Bedrock: the cross-region inference profile (e.g.
        ``us.anthropic.claude-sonnet-4-6-v1:0``). For Vertex: the
        revision tag. For direct: the canonical model name unchanged.

    Raises:
        KeyError: if the logical ID is not a known Cortex model tier.
    """
    p = _resolve_provider(provider)
    if p == "bedrock":
        return _BEDROCK_INFERENCE_PROFILES[logical]
    if p == "vertex":
        return _VERTEX_MODEL_IDS[logical]
    return logical


def build_anthropic_sdk_client(
    *,
    provider: str | None = None,
    bedrock_region: str | None = None,
    bedrock_bearer_token: str | None = None,
    anthropic_api_key: str | None = None,
    vertex_region: str | None = None,
) -> AsyncAnthropic | AsyncAnthropicBedrock | AsyncAnthropicVertex:
    """Construct the right Anthropic async client for the current provider.

    The resulting object exposes the unified ``messages.create(...)`` API,
    so callers (the Cortex planner) are provider-agnostic.

    Args:
        provider: Override for ``ANTHROPIC_PROVIDER`` env var.
        bedrock_region: AWS region; defaults to ``AWS_REGION`` env or
            ``us-east-2``.
        bedrock_bearer_token: Long-lived Bedrock bearer token; defaults to
            ``AWS_BEARER_TOKEN_BEDROCK`` env var.
        anthropic_api_key: Direct Anthropic API key; defaults to
            ``ANTHROPIC_API_KEY`` env var.
        vertex_region: GCP region for Vertex; defaults to
            ``GOOGLE_CLOUD_REGION`` env or ``us-east5``.

    Returns:
        The configured async client.

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
                "AWS_BEARER_TOKEN_BEDROCK is not set; cannot build Bedrock "
                "client. Run the BYOK step in onboarding or export the env "
                "var before launching the daemon.",
            )
        # The Anthropic SDK reads the bearer token from the env var
        # directly. Setting aws_region picks the right endpoint.
        return AsyncAnthropicBedrock(aws_region=region)

    if p == "vertex":
        region = vertex_region or os.getenv("GOOGLE_CLOUD_REGION") or "us-east5"
        return AsyncAnthropicVertex(region=region)

    # direct
    api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing; cannot build direct Anthropic client.",
        )
    return AsyncAnthropic(api_key=api_key)
