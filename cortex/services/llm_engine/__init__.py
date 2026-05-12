"""LLM Engine — Anthropic SDK production path.

The Cortex daemon always interacts with Claude through the
:class:`LLMClient` Protocol. In v0.2.0 the legacy Azure / remote Qwen /
local Ollama transports were retired; the single production
implementation is :class:`AnthropicPlanner`, which wraps the Anthropic
SDK and selects ``AsyncAnthropicBedrock`` / ``AsyncAnthropic`` /
``AsyncAnthropicVertex`` via the ``ANTHROPIC_PROVIDER`` env var.
"""

from cortex.libs.config.settings import LLMConfig
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import (
    LLMClient,
    LLMError,
    RuleBasedLLMClient,
    build_fallback_plan,
)
from cortex.services.llm_engine.parser import (
    parse_and_validate,
    parse_llm_response,
    validate_intervention_plan,
)
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    SYSTEM_PROMPT,
    build_anthropic_messages,
    build_messages,
    build_user_prompt,
    select_prompt_template,
)

__all__ = [
    "AnthropicPlanner",
    "LLMCache",
    "LLMClient",
    "LLMError",
    "PROMPT_TEMPLATES",
    "RuleBasedLLMClient",
    "SYSTEM_PROMPT",
    "build_anthropic_messages",
    "build_fallback_plan",
    "build_messages",
    "build_user_prompt",
    "create_llm_client",
    "parse_and_validate",
    "parse_llm_response",
    "select_prompt_template",
    "validate_intervention_plan",
]


def create_llm_client(
    config: LLMConfig | None = None,
) -> LLMClient:
    """Construct the production LLM client.

    Always returns an :class:`AnthropicPlanner` unless the user has
    explicitly opted in to ``fallback_mode="rule_based"`` AND no Bedrock
    bearer token is available — in that case the deterministic
    :class:`RuleBasedLLMClient` is returned so the daemon never crashes.
    """
    cfg = config or LLMConfig()

    if cfg.fallback_mode == "rule_based":
        # The planner itself degrades gracefully on circuit-open / credential
        # failure, so this branch is reached only when callers opt out of
        # any Anthropic call (e.g. tests, air-gapped environments).
        import os

        if cfg.provider == "bedrock" and not os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
            try:
                return AnthropicPlanner(cfg)
            except RuntimeError:
                return RuleBasedLLMClient()

    return AnthropicPlanner(cfg)
