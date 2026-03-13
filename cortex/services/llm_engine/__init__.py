# LLM Engine - Remote Qwen-3-8B client

from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import LLMClient, LLMError, RuleBasedLLMClient, build_fallback_plan
from cortex.services.llm_engine.azure_openai import AzureOpenAIClient
from cortex.services.llm_engine.local_ollama import LocalOllamaClient
from cortex.services.llm_engine.parser import parse_and_validate, parse_llm_response, validate_intervention_plan
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    SYSTEM_PROMPT,
    build_messages,
    build_user_prompt,
    select_prompt_template,
)
from cortex.services.llm_engine.remote_qwen import RemoteQwenClient
from cortex.libs.config.settings import LLMConfig

__all__ = [
    "AzureOpenAIClient",
    "LLMCache",
    "LLMClient",
    "LLMError",
    "LocalOllamaClient",
    "PROMPT_TEMPLATES",
    "RemoteQwenClient",
    "RuleBasedLLMClient",
    "SYSTEM_PROMPT",
    "build_fallback_plan",
    "build_messages",
    "build_user_prompt",
    "parse_and_validate",
    "parse_llm_response",
    "select_prompt_template",
    "validate_intervention_plan",
    "create_llm_client",
]


def create_llm_client(
    config: LLMConfig | None = None,
) -> LLMClient | AzureOpenAIClient | LocalOllamaClient | RemoteQwenClient | RuleBasedLLMClient:
    """Create the configured LLM client for runtime use."""
    cfg = config or LLMConfig()
    if cfg.mode == "azure":
        return AzureOpenAIClient(cfg)
    if cfg.mode == "local":
        return LocalOllamaClient(cfg)
    if cfg.mode == "remote":
        return RemoteQwenClient(cfg)
    if cfg.mode == "rule_based":
        return RuleBasedLLMClient()
    return AzureOpenAIClient(cfg)
