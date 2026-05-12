"""Anthropic SDK transport — provider-routed Claude API access.

This package provides a single entry point for constructing the appropriate
Anthropic async client based on the ``ANTHROPIC_PROVIDER`` environment
variable (default ``bedrock``). The Cortex daemon never depends on a
specific transport — all Claude calls go through this layer.
"""

from cortex.libs.llm.anthropic_client import (
    build_anthropic_sdk_client,
    resolve_anthropic_model_id,
)

__all__ = ["build_anthropic_sdk_client", "resolve_anthropic_model_id"]
