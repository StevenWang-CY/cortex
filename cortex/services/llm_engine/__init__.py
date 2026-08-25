"""LLM Engine — Anthropic SDK production path.

The Cortex daemon always interacts with Claude through the
:class:`LLMClient` Protocol. In v0.2.0 the legacy Azure / remote Qwen /
local Ollama transports were retired; the single production
implementation is :class:`AnthropicPlanner`, which wraps the Anthropic
SDK and selects ``AsyncAnthropicBedrock`` / ``AsyncAnthropic`` /
``AsyncAnthropicVertex`` via the ``ANTHROPIC_PROVIDER`` env var.
"""

from cortex.application.clock import Clock
from cortex.libs.config.settings import LLMConfig
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import (
    LLMClient,
    LLMError,
    RuleBasedLLMClient,
    build_fallback_plan,
)
from cortex.services.llm_engine.context_broker import (
    ContextBroker,
    NoContentPlanner,
    PrivacyAwarePlanner,
    build_no_content_plan,
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
    "ContextBroker",
    "NoContentPlanner",
    "PrivacyAwarePlanner",
    "SYSTEM_PROMPT",
    "build_anthropic_messages",
    "build_fallback_plan",
    "build_messages",
    "build_no_content_plan",
    "build_user_prompt",
    "create_llm_client",
    "parse_and_validate",
    "parse_llm_response",
    "select_prompt_template",
    "validate_intervention_plan",
]


def create_llm_client(
    config: LLMConfig | None = None,
    *,
    clock: Clock | None = None,
) -> LLMClient:
    """Construct the production LLM client.

    Network access is opt-in. The default ``no_llm`` mode constructs no SDK
    and uses the deterministic local rule planner. ``no_content`` constructs
    neither an SDK nor a context-dependent planner. External mode is always
    wrapped in :class:`PrivacyAwarePlanner`, which requires a fresh exact
    preview confirmation for every request.
    """
    cfg = config or LLMConfig()

    if cfg.privacy.planner_mode == "no_content":
        return NoContentPlanner()

    if cfg.privacy.planner_mode == "no_llm":
        return RuleBasedLLMClient()

    if not cfg.privacy.external_transport_enabled:
        return PrivacyAwarePlanner(cfg, None, clock=clock)

    try:
        transport = AnthropicPlanner(cfg, clock=clock)
    except RuntimeError:
        transport = None
    return PrivacyAwarePlanner(cfg, transport, clock=clock)
