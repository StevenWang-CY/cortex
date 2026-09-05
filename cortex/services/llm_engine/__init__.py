"""LLM Engine — Anthropic SDK production path.

The Cortex daemon always interacts with Claude through the
:class:`LLMClient` Protocol. The single production implementation is
:class:`AnthropicPlanner`, which wraps the Anthropic SDK and selects
``AsyncAnthropicBedrockMantle`` / ``AsyncAnthropic`` /
``AsyncAnthropicVertex`` via ``LLMConfig.provider``. Every request uses
structured outputs (:mod:`plan_draft`) — no tools, no sampling params.
"""

import logging

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
from cortex.services.llm_engine.plan_draft import (
    PlanDraft,
    draft_to_plan_data,
    structured_output_schema,
)
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    SYSTEM_PROMPT,
    build_anthropic_messages,
    build_messages,
    build_user_prompt,
    select_prompt_template,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicPlanner",
    "LLMCache",
    "LLMClient",
    "LLMError",
    "PROMPT_TEMPLATES",
    "PlanDraft",
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
    "draft_to_plan_data",
    "parse_and_validate",
    "parse_llm_response",
    "select_prompt_template",
    "structured_output_schema",
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

    When external mode is configured but the provider credential is not
    yet available (first-run BYOK), the wrapper is created without a
    transport and given a factory so ``reload_credentials()`` can build it
    later without a daemon restart.
    """
    cfg = config or LLMConfig()

    if cfg.privacy.planner_mode == "no_content":
        return NoContentPlanner()

    if cfg.privacy.planner_mode == "no_llm":
        return RuleBasedLLMClient()

    if not cfg.privacy.external_transport_enabled:
        return PrivacyAwarePlanner(cfg, None, clock=clock)

    def _build_transport() -> AnthropicPlanner:
        # The only production call site of the transport constructor
        # (``test_production_modules_construct_anthropic_only_in_composition_root``).
        return AnthropicPlanner(cfg, clock=clock)

    transport: AnthropicPlanner | None
    try:
        transport = _build_transport()
    except RuntimeError as exc:
        logger.warning(
            "External planner credentials missing (%s); the transport will be "
            "constructed on reload_credentials() after the BYOK step",
            exc,
        )
        transport = None
    return PrivacyAwarePlanner(cfg, transport, clock=clock, transport_factory=_build_transport)
