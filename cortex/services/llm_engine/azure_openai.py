"""
LLM Engine — Azure OpenAI Client

Uses Azure OpenAI chat completions as the primary production planner with
local Ollama fallback and finally rule-based fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.utils import get_keychain_password
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import LLMError, build_fallback_plan
from cortex.services.llm_engine.local_ollama import LocalOllamaClient
from cortex.services.llm_engine.parser import parse_and_validate
from cortex.services.llm_engine.prompts import build_messages

logger = logging.getLogger(__name__)


class AzureOpenAIClient:
    """Azure OpenAI-backed intervention planner."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: LLMCache | None = None,
        ollama_client: LocalOllamaClient | None = None,
    ) -> None:
        self._config = config or LLMConfig()
        self._cache = cache or LLMCache()
        self._ollama = ollama_client or LocalOllamaClient(config=self._config, cache=self._cache)
        self._max_retries = 2

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
    ) -> InterventionPlan:
        """Generate an intervention plan via Azure OpenAI."""
        cached = self._cache.get(context, state, constraints)
        if cached is not None:
            return cached

        messages = build_messages(context, state, constraints)
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                raw_response = await self._call_api(messages, intervention_level=state.state)
                plan = parse_and_validate(raw_response)
                if plan is not None:
                    self._cache.put(context, plan, state, constraints)
                    return plan
                logger.warning(
                    "Azure parse/validate failed on attempt %d: %s",
                    attempt,
                    raw_response[:200] if raw_response else "<empty>",
                )
            except (httpx.HTTPError, TimeoutError, OSError, LLMError) as exc:
                last_error = exc
                logger.warning("Azure API call failed on attempt %d: %s", attempt, exc)

        if self._config.fallback_mode == "local_ollama":
            try:
                return await self._ollama.generate_intervention_plan(context, state, constraints)
            except Exception as exc:  # pragma: no cover - defensive fallback
                last_error = exc
                logger.warning("Azure fallback to Ollama failed: %s", exc)

        if last_error is not None:
            logger.error("Azure planning failed, using rule-based fallback: %s", last_error)
        return build_fallback_plan(context)

    async def health_check(self) -> bool:
        """Check whether the Azure endpoint and credentials are usable."""
        endpoint = self._normalized_endpoint
        api_key = self._api_key
        if not endpoint or not api_key:
            return False

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{endpoint}/openai/models",
                    params={"api-version": self._config.azure.api_version},
                    headers={"api-key": api_key},
                )
            return response.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    @property
    def _normalized_endpoint(self) -> str:
        return self._config.azure.endpoint.rstrip("/")

    @property
    def _api_key(self) -> str:
        if self._config.azure.api_key:
            return self._config.azure.api_key
        if not self._config.azure.use_keychain:
            return ""
        return (
            get_keychain_password(
                self._config.azure.keychain_service,
                self._config.azure.keychain_account,
            )
            or ""
        )

    def _deployment_for_level(self, intervention_level: str) -> str:
        if intervention_level in {"guided_mode", "HYPER"} and self._config.azure.reasoning_deployment_name:
            return self._config.azure.reasoning_deployment_name
        return self._config.azure.deployment_name

    async def _call_api(
        self,
        messages: list[dict[str, str]],
        *,
        intervention_level: str,
    ) -> str:
        endpoint = self._normalized_endpoint
        api_key = self._api_key
        deployment = self._deployment_for_level(intervention_level)
        if not endpoint or not api_key or not deployment:
            raise LLMError("Azure OpenAI is not fully configured")

        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": self._config.temperature,
            "max_completion_tokens": max(
                self._config.azure.max_completion_tokens, 2048
            ),
            "stream": False,
        }

        timeout = httpx.Timeout(self._config.timeout_seconds, connect=5.0)
        url = (
            f"{endpoint}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={self._config.azure.api_version}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"api-key": api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMError("No choices in Azure response")

        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                return "".join(parts)

        raise LLMError("Empty content in Azure response")

