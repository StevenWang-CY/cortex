"""Anthropic SDK intervention planner — production LLM path.

Single concrete implementation of the ``LLMClient`` Protocol. Replaces
the deprecated Azure / Qwen / Ollama clients. Uses ``AsyncAnthropicBedrock``
as the production transport with ``AsyncAnthropic`` / ``AsyncAnthropicVertex``
as drop-in escape hatches selected by ``ANTHROPIC_PROVIDER``.

Design notes
------------
* **Structured output via tool-use.** The Anthropic Messages API has no
  ``response_format: json_object`` equivalent. Instead we attach the
  Pydantic-derived schema as a forced tool call. The model returns a
  ``tool_use`` block whose ``input`` is a typed dict matching
  :class:`InterventionPlan`.
* **Per-template model tier.** Latency-critical short outputs go to
  Haiku; standard planning to Sonnet; multi-step debugging to Opus.
  Configurable via ``LLMConfig.template_tier_overrides``.
* **Prompt caching.** The 3-4k-token system prompt is marked
  ``cache_control: ephemeral`` so back-to-back interventions reuse it.
* **Resilience.** Per-call retries with bounded exponential jitter, a
  consecutive-failure circuit breaker, and a bounded semaphore that caps
  in-flight Bedrock concurrency. Graceful degrade to
  :func:`build_fallback_plan` (deterministic, schema-valid).
* **Observability.** Each call emits a structured ``llm.request`` log
  event (model id, template, latency, cache hit/write, status).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from typing import Any, Literal, cast

import keyring
from anthropic import APIError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import ValidationError

from cortex.libs.config.settings import LLMConfig
from cortex.libs.llm.anthropic_client import (
    LogicalModel,
    build_anthropic_sdk_client,
    resolve_anthropic_model_id,
)
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SimplificationConstraints,
)
from cortex.libs.schemas.state import StateEstimate
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import build_fallback_plan
from cortex.services.llm_engine.parser import (
    enrich_plan_with_context,
    validate_intervention_plan,
)
from cortex.services.llm_engine.prompts import build_anthropic_messages

logger = logging.getLogger(__name__)

ModelTier = Literal["fast", "default", "deep"]

# Map every Cortex prompt template to a model tier. Latency-critical
# short outputs use Haiku; multi-step causal reasoning uses Opus.
_TEMPLATE_TIER: dict[str, ModelTier] = {
    "calm_overlay_writer": "fast",
    "browser_tab_reduction": "fast",
    "micro_step_planner": "default",
    "code_focus_reduction": "default",
    "debug_error_summary": "deep",
    "causal_explanation_grounding": "fast",
    "prompt_text_sanitizer": "fast",
    # v2 templates
    "active_recall": "default",
    "morning_briefing": "fast",
    "pre_break_warning": "fast",
    "breathing_overlay": "fast",
    "rabbit_hole_intervention": "default",
}

# Tool definition forcing the model to emit a structured plan.
_PLAN_TOOL_NAME = "emit_intervention_plan"


def _make_intervention_plan_tool() -> dict[str, Any]:
    """Build the Anthropic tool definition from the Pydantic schema.

    ``InterventionPlan.model_json_schema()`` produces a $ref-rich
    schema. Anthropic accepts that as long as it's a valid JSONSchema
    object (the SDK doesn't try to resolve refs itself — it forwards
    them to the model). We attach ``cache_control`` so the tool spec
    is included in the ephemeral prompt cache alongside the system
    prompt.
    """
    schema = InterventionPlan.model_json_schema()
    return {
        "name": _PLAN_TOOL_NAME,
        "description": (
            "Emit a structured intervention plan that the Cortex daemon "
            "will execute against the user's workspace. Always call this "
            "tool — never reply with plain text."
        ),
        "input_schema": schema,
        "cache_control": {"type": "ephemeral"},
    }


def _extract_tool_use_input(response: Any) -> dict[str, Any]:
    """Pull the dict the model passed to ``emit_intervention_plan``.

    Raises:
        ValueError: when no tool_use block is present or it targets the
            wrong tool name.
    """
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _PLAN_TOOL_NAME:
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                return payload
    raise ValueError(
        f"Anthropic response missing tool_use({_PLAN_TOOL_NAME!r}) block",
    )


def _keychain_get_bedrock_token(config: LLMConfig) -> str | None:
    """Fetch the Bedrock bearer token from the macOS Keychain.

    Returns ``None`` when keyring is unavailable or no entry exists,
    in which case the SDK reads ``AWS_BEARER_TOKEN_BEDROCK`` from env.
    """
    if not config.use_keychain or config.provider != "bedrock":
        return None
    try:
        return keyring.get_password(
            config.bedrock.keychain_service,
            config.bedrock.keychain_account,
        )
    except Exception:  # noqa: BLE001 — keyring backend missing on Linux/Windows
        return None


class _CircuitBreaker:
    """Trip on consecutive failures; auto-close after a cooldown."""

    def __init__(self, threshold: int, window_seconds: float, open_seconds: float) -> None:
        self._threshold = max(1, threshold)
        self._window = max(1.0, window_seconds)
        self._open_seconds = max(1.0, open_seconds)
        self._failures: deque[float] = deque(maxlen=64)
        self._opened_at: float | None = None

    def allow(self, now: float) -> bool:
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self._open_seconds:
            # Half-open: allow one probe.
            self._opened_at = None
            self._failures.clear()
            return True
        return False

    def record_failure(self, now: float) -> None:
        self._failures.append(now)
        # Drop stale entries outside the rolling window.
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        if len(self._failures) >= self._threshold:
            self._opened_at = now
            logger.warning(
                "Anthropic circuit opened after %d failures in %.0fs",
                len(self._failures),
                self._window,
            )

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None


class AnthropicPlanner:
    """Production LLM client backed by the Anthropic SDK.

    Implements the :class:`cortex.services.llm_engine.client.LLMClient`
    Protocol. Tests inject a stub via the ``sdk`` keyword argument.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: LLMCache | None = None,
        *,
        sdk: Any | None = None,
    ) -> None:
        self._config = config or LLMConfig()

        if self._config.provider == "bedrock":
            token = _keychain_get_bedrock_token(self._config)
            if token and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
                # Surface to env so the SDK constructor can read it.
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token

        self._sdk = sdk or build_anthropic_sdk_client(
            provider=self._config.provider,
            bedrock_region=self._config.bedrock.aws_region,
        )

        # Resolve each tier's provider-specific model identifier once.
        self._models: dict[ModelTier, str] = {
            "fast": resolve_anthropic_model_id(
                cast(LogicalModel, self._config.model_fast),
                provider=self._config.provider,
            ),
            "default": resolve_anthropic_model_id(
                cast(LogicalModel, self._config.model_default),
                provider=self._config.provider,
            ),
            "deep": resolve_anthropic_model_id(
                cast(LogicalModel, self._config.model_deep),
                provider=self._config.provider,
            ),
        }

        self._cache = cache or LLMCache(default_ttl=self._config.cache_ttl_seconds)
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_requests)
        self._circuit = _CircuitBreaker(
            threshold=self._config.circuit_failure_threshold,
            window_seconds=self._config.circuit_window_seconds,
            open_seconds=self._config.circuit_open_seconds,
        )
        self._plan_tool = _make_intervention_plan_tool()

    def _select_tier(self, template_name: str | None) -> ModelTier:
        if template_name:
            overrides = self._config.template_tier_overrides
            if template_name in overrides:
                return overrides[template_name]
            if template_name in _TEMPLATE_TIER:
                return _TEMPLATE_TIER[template_name]
        return "default"

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
    ) -> InterventionPlan:
        """Generate a typed intervention plan, with cache + retry + fallback."""
        now_mono = time.monotonic()

        # Cache hit short-circuits everything.
        cached = self._cache.get(context, state, constraints, now=now_mono)
        if cached is not None:
            logger.debug("LLM cache hit (template=%s)", template_name)
            return cached

        if not self._circuit.allow(now_mono):
            logger.warning("LLM circuit open; serving deterministic fallback")
            return build_fallback_plan(context)

        tier = self._select_tier(template_name)
        model_id = self._models[tier]
        system_blocks, messages = build_anthropic_messages(
            context,
            state,
            constraints,
            template_name=template_name,
            extra_context=extra_context,
        )

        attempts = 3
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                async with self._semaphore:
                    # swift-concurrency-pro rule (transferred to asyncio):
                    # shield the Bedrock call from cooperative cancellation.
                    # If the caller cancels mid-flight (state pipeline
                    # tear-down, daemon SIGTERM), we still let the SDK
                    # finish its current HTTP transaction cleanly so the
                    # Bedrock connection isn't left in a half-open state.
                    response = await asyncio.shield(
                        self._sdk.messages.create(
                            model=model_id,
                            max_tokens=self._config.max_tokens,
                            temperature=self._config.temperature,
                            system=system_blocks,
                            messages=messages,
                            tools=[self._plan_tool],
                            tool_choice={
                                "type": "tool",
                                "name": _PLAN_TOOL_NAME,
                            },
                            timeout=self._config.timeout_seconds,
                        )
                    )
            except (RateLimitError, APITimeoutError, APIStatusError) as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                logger.warning(
                    "llm.request status=error model=%s template=%s "
                    "latency_ms=%.0f attempt=%d err=%s",
                    model_id,
                    template_name,
                    latency_ms,
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt == attempts - 1:
                    self._circuit.record_failure(time.monotonic())
                    break
                # Bounded exponential backoff with jitter.
                await asyncio.sleep(min(2 ** attempt + random.random(), 8.0))
                continue
            except APIError as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                logger.error(
                    "llm.request status=fatal model=%s template=%s "
                    "latency_ms=%.0f err=%s",
                    model_id,
                    template_name,
                    latency_ms,
                    type(exc).__name__,
                )
                self._circuit.record_failure(time.monotonic())
                break

            # Successful HTTP — now validate the tool_use payload.
            try:
                tool_input = _extract_tool_use_input(response)
                plan = validate_intervention_plan(tool_input)
                if plan is None:
                    raise ValidationError.from_exception_data(
                        title="InterventionPlan",
                        line_errors=[],
                    )
            except (ValueError, ValidationError) as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                logger.warning(
                    "llm.request status=invalid model=%s template=%s "
                    "latency_ms=%.0f err=%s",
                    model_id,
                    template_name,
                    latency_ms,
                    type(exc).__name__,
                )
                if attempt == attempts - 1:
                    self._circuit.record_failure(time.monotonic())
                    break
                continue

            self._circuit.record_success()
            latency_ms = (time.perf_counter() - t0) * 1000.0
            usage = getattr(response, "usage", None)
            logger.info(
                "llm.request status=ok model=%s template=%s tier=%s "
                "latency_ms=%.0f tokens_in=%s tokens_out=%s "
                "cache_read=%s cache_write=%s",
                model_id,
                template_name,
                tier,
                latency_ms,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
            )

            enriched = enrich_plan_with_context(plan, context)
            # D.6: surface the simplification constraint window into the
            # UIPlan so VS Code can size its fold window per-plan instead
            # of using the hard-coded ±20 line default.
            if constraints is not None and enriched.ui_plan is not None:
                try:
                    half = max(5, int(constraints.max_visible_lines) // 2)
                    enriched.ui_plan.max_visible_lines = half
                except Exception:
                    pass
            self._cache.put(context, enriched, state, constraints)
            return enriched

        # All retries exhausted → deterministic fallback.
        logger.warning(
            "LLM call exhausted retries for template=%s; using fallback",
            template_name,
        )
        return build_fallback_plan(context)

    async def health_check(self) -> bool:
        """Cheap readiness check — never crash the daemon if the SDK is down."""
        try:
            # No dedicated ping endpoint on Anthropic; just probe model resolution.
            _ = self._models["default"]
            return True
        except Exception:  # noqa: BLE001
            return False
