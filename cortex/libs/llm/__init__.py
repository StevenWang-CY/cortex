"""Anthropic SDK transport — provider-routed Claude API access.

This package provides a single entry point for constructing the appropriate
Anthropic async client based on ``LLMConfig.provider`` (or the
``ANTHROPIC_PROVIDER`` environment variable, default ``bedrock``), plus the
per-model capability table and pricing used by the planner. The Cortex
daemon never depends on a specific transport — all Claude calls go through
this layer.
"""

from cortex.libs.llm.anthropic_client import (
    MODEL_CAPABILITIES,
    ModelCapabilities,
    build_anthropic_sdk_client,
    model_capabilities,
    resolve_anthropic_model_id,
)
from cortex.libs.llm.pricing import normalize_model_id, usd_cost

__all__ = [
    "MODEL_CAPABILITIES",
    "ModelCapabilities",
    "build_anthropic_sdk_client",
    "model_capabilities",
    "normalize_model_id",
    "resolve_anthropic_model_id",
    "usd_cost",
]
