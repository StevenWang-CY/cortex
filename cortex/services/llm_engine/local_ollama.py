"""
LLM Engine — Local Ollama Fallback Client

Uses a local Ollama instance as a fallback when the remote Qwen server is
unreachable. Communicates via the Ollama REST API (OpenAI-compatible endpoint).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import build_fallback_plan
from cortex.services.llm_engine.parser import parse_and_validate
from cortex.services.llm_engine.prompts import build_messages

logger = logging.getLogger(__name__)


class LocalOllamaClient:
    """
    LLM client that talks to a local Ollama instance.

    Implements the LLMClient protocol.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        if config is None:
            config = LLMConfig()
        self._config = config
        self._cache = cache or LLMCache()
        self._base_url = f"http://{config.local.host}:{config.local.port}"
        self._model = config.local.model
        self._max_retries = 2

    # ------------------------------------------------------------------
    # LLMClient Protocol
    # ------------------------------------------------------------------

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
    ) -> InterventionPlan:
        """Generate an intervention plan via local Ollama."""
        # Check cache first
        cached = self._cache.get(context, state, constraints)
        if cached is not None:
            return cached

        messages = build_messages(
            context,
            state,
            constraints,
            template_name=template_name,
            extra_context=extra_context,
        )

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                raw_response = await self._call_api(messages)
                plan = parse_and_validate(raw_response)
                if plan is not None:
                    self._cache.put(context, plan, state, constraints)
                    return plan
                logger.warning(
                    "Parse/validate failed on attempt %d: %s",
                    attempt,
                    raw_response[:200] if raw_response else "<empty>",
                )
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                last_error = exc
                logger.warning(
                    "Ollama API call failed on attempt %d: %s", attempt, exc
                )

        logger.error(
            "Ollama call failed after %d retries, using fallback plan",
            self._max_retries,
        )
        if last_error is not None:
            logger.error("Last error: %s", last_error)

        return build_fallback_plan(context)

    async def health_check(self) -> bool:
        """Check if the local Ollama server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    # ------------------------------------------------------------------
    # Internal API call
    # ------------------------------------------------------------------

    async def _call_api(self, messages: list[dict[str, str]]) -> str:
        """
        Make a single call to the Ollama OpenAI-compatible chat API.

        Returns the raw content string from the response.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "num_predict": self._config.max_tokens,
                "temperature": self._config.temperature,
            },
        }

        timeout = httpx.Timeout(
            self._config.timeout_seconds * 3,  # Ollama is slower
            connect=5.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._base_url}/api/chat",
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        message = data.get("message", {})
        content = message.get("content", "")

        if not content:
            # Try OpenAI-compatible format fallback
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")

        return content
