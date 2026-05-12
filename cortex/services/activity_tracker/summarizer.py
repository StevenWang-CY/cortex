"""
Activity Summarizer

Generates LLM-powered context recaps for learning activities to help
students remember where they left off and what concepts were being covered.
"""

from __future__ import annotations

import logging

from cortex.libs.schemas.activity import ActivitySummary

logger = logging.getLogger(__name__)

# Cache TTL: 7 days
_RECAP_TTL = 7 * 86400

_RECAP_PROMPT = """\
The student was studying "{title}" on {platform}. \
They stopped at {position}. \
The content they were viewing: "{context}". \
Write a concise 1-2 sentence recap to help them remember where they left off \
and what concept was being covered. Be specific and helpful."""


class ActivitySummarizer:
    """Generates and caches LLM context recaps for activities."""

    def __init__(self, store: object, llm_config: object | None = None) -> None:
        """
        Args:
            store: A store instance with get_json/set_json methods.
            llm_config: Optional LLMConfig for Azure OpenAI calls.
        """
        self._store = store
        self._llm_config = llm_config

    async def get_recap(self, activity: ActivitySummary) -> str:
        """Get or generate a context recap for an activity.

        Returns a cached recap if available, otherwise generates one via LLM.
        Falls back to a simple template if LLM is unavailable.
        """
        cache_key = f"activity:resume_context:{activity.content_id}"
        cached = await self._store.get_json(cache_key)
        if cached and isinstance(cached, dict) and cached.get("recap"):
            return cached["recap"]

        recap = await self._generate_recap(activity)

        # Cache the result
        await self._store.set_json(cache_key, {"recap": recap}, ttl_seconds=_RECAP_TTL)
        return recap

    async def _generate_recap(self, activity: ActivitySummary) -> str:
        """Generate a recap using LLM or fall back to a template."""
        if self._llm_config is not None:
            try:
                return await self._call_llm(activity)
            except Exception as exc:
                logger.debug("LLM recap generation failed, using template: %s", exc)

        return self._template_recap(activity)

    async def _call_llm(self, activity: ActivitySummary) -> str:
        """Generate a recap via the Anthropic SDK (Haiku tier).

        v0.2.1: rewritten from the Azure OpenAI fetch above to call Bedrock
        through the shared ``cortex.libs.llm.anthropic_client`` factory.
        We don't go through ``AnthropicPlanner`` because that path forces
        a typed ``InterventionPlan`` tool-use output; for a 1-3 sentence
        recap we just want raw text.
        """
        from cortex.libs.config.settings import LLMConfig
        from cortex.libs.llm.anthropic_client import (
            build_anthropic_sdk_client,
            resolve_anthropic_model_id,
        )

        config = self._llm_config
        if not isinstance(config, LLMConfig):
            raise ValueError("LLM config not available")

        prompt = _RECAP_PROMPT.format(
            title=activity.title,
            platform=activity.platform,
            position=activity.position_description or "unknown position",
            context=activity.context_snapshot[:200] if activity.context_snapshot else "N/A",
        )

        try:
            sdk = build_anthropic_sdk_client(
                provider=config.provider,
                bedrock_region=config.bedrock.aws_region,
            )
        except RuntimeError as exc:
            # No credentials available → fall through to template recap.
            raise ValueError(str(exc)) from exc

        model_id = resolve_anthropic_model_id(
            config.model_fast, provider=config.provider,
        )
        response = await sdk.messages.create(
            model=model_id,
            max_tokens=200,
            temperature=0.3,
            system=[{"type": "text", "text": "You are a concise study assistant."}],
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            timeout=15.0,
        )
        # Concatenate any text blocks the model returns.
        parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _template_recap(activity: ActivitySummary) -> str:
        """Simple template-based recap when LLM is unavailable."""
        parts = [f"You were studying \"{activity.title}\" on {activity.platform}."]
        if activity.position_description:
            parts.append(f"You stopped at {activity.position_description}.")
        if activity.completion_pct > 0:
            parts.append(f"Progress: {activity.completion_pct:.0f}% complete.")
        return " ".join(parts)
