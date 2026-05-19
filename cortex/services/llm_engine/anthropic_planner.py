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

from anthropic import APIError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import ValidationError

# audit Phase-I: ``keyring`` is imported lazily inside
# :func:`_keychain_get_bedrock_token`. The module is heavyweight
# (~80 ms cold import on macOS — it scans the keyring backend entry
# points via ``importlib.metadata``) and we only need it the first
# time the planner is asked to mint a Bedrock client. Deferring the
# import shaves measurable time off daemon startup. The regression
# guard lives in ``cortex/tests/performance/test_startup_latency.py``.
from cortex.libs.config.settings import LLMConfig
from cortex.libs.llm.anthropic_client import (
    LogicalModel,
    build_anthropic_sdk_client,
    resolve_anthropic_model_id,
)
from cortex.libs.llm.pricing import usd_cost
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SimplificationConstraints,
)
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.utils.platform import get_config_dir
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import build_fallback_plan
from cortex.services.llm_engine.cost_tracker import CostTracker
from cortex.services.llm_engine.parser import (
    enrich_plan_with_context,
    validate_intervention_plan,
)
from cortex.services.llm_engine.prompts import (
    build_anthropic_messages,
    capture_truncation_report,
)

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


def _estimate_request_input_tokens(
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> int:
    """Best-effort input-token estimate for the assembled request.

    Used by F30's cancellation cost path: if the shielded Bedrock call
    is cancelled before any response arrives, ``response.usage`` is
    unavailable, so we approximate from the request payload. The chars/4
    heuristic matches :func:`cortex.services.llm_engine.prompts._estimate_tokens`
    so the two layers agree on what "a token" means.
    """
    total_chars = 0
    for block in system_blocks or []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            total_chars += len(text)
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for sub in content:
                if isinstance(sub, dict):
                    sub_text = sub.get("text")
                    if isinstance(sub_text, str):
                        total_chars += len(sub_text)
    return max(0, total_chars // 4)


def _keychain_get_bedrock_token(config: LLMConfig) -> str | None:
    """Fetch the Bedrock bearer token from the macOS Keychain.

    Returns ``None`` when keyring is unavailable or no entry exists,
    in which case the SDK reads ``AWS_BEARER_TOKEN_BEDROCK`` from env.

    audit Phase-I: ``keyring`` is imported lazily here (rather than at
    module top) so importing :mod:`anthropic_planner` does not drag the
    keyring backend discovery into daemon startup. The keychain lookup
    only happens when the planner actually mints an LLM client.
    """
    if not config.use_keychain or config.provider != "bedrock":
        return None
    try:
        import keyring  # noqa: PLC0415 — intentional lazy import

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
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self._config = config or LLMConfig()

        # F11: previously the keychain-sourced Bedrock token was written
        # to ``os.environ`` permanently, which then propagated to every
        # subprocess the daemon spawned (capture worker, native host
        # re-launches, project launcher terminals). A debugger or
        # crash-dump tool attached to any descendant could read it.
        # The Anthropic SDK reads ``AWS_BEARER_TOKEN_BEDROCK`` at
        # construction time only, so we narrow the env mutation to that
        # window and restore the prior value (or unset) on exit.
        if sdk is None and self._config.provider == "bedrock":
            keychain_token = (
                _keychain_get_bedrock_token(self._config)
                if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                else None
            )
            prior = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            try:
                if keychain_token:
                    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = keychain_token
                self._sdk = build_anthropic_sdk_client(
                    provider=self._config.provider,
                    bedrock_region=self._config.bedrock.aws_region,
                )
            finally:
                if keychain_token:
                    # Restore the prior state precisely: re-set or unset.
                    if prior is None:
                        os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
                    else:
                        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = prior
        else:
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

        # F20: per-day USD spend ledger + kill-switch. Use the injected
        # tracker in tests; in production fall back to the per-user
        # config-dir ledger so spend survives across daemon restarts.
        if cost_tracker is not None:
            self._cost_tracker: CostTracker | None = cost_tracker
        else:
            try:
                ledger_path = get_config_dir() / "cost_ledger.json"
                self._cost_tracker = CostTracker(
                    ledger_path=ledger_path,
                    warn_usd=self._config.cost_warn_usd,
                    kill_usd=self._config.daily_cost_budget_usd,
                )
            except (OSError, ValueError) as exc:
                # Cost tracking is best-effort: a broken ledger path
                # must not break the planner. The daemon logs the issue
                # but continues; spend will be invisible until the path
                # is made writable.
                logger.warning(
                    "cost_tracker: disabled (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                self._cost_tracker = None

    def reload_credentials(self) -> bool:
        """Rebuild the SDK client using the latest BYOK token.

        Audit-2 fix: previously the keychain-sourced Bedrock token was
        read once at planner construction. After the onboarding "save
        token" step or a Settings "rotate token" action, the running
        planner kept using the prior cached SDK client (or no client at
        all for first-run installs), so the very next intervention
        silently fell through to the rule-based fallback even though
        the user had just supplied a valid token.

        Returns True if a working SDK client was constructed, False
        if no token is available or the rebuild raised. Callers can
        surface a UI toast on failure.
        """
        if self._config.provider != "bedrock":
            # Vertex / Direct providers acquire credentials via Google
            # ADC / ANTHROPIC_API_KEY env at SDK build; rebuild is still
            # useful so we honour any env mutation the caller performed.
            try:
                self._sdk = build_anthropic_sdk_client(
                    provider=self._config.provider,
                    bedrock_region=self._config.bedrock.aws_region,
                )
                self._cache.clear()
                logger.info("Planner SDK rebuilt for provider=%s", self._config.provider)
                return True
            except Exception:
                logger.exception("Planner SDK rebuild failed")
                return False

        keychain_token = _keychain_get_bedrock_token(self._config)
        if not keychain_token:
            logger.warning("reload_credentials: no Bedrock token in keychain")
            return False
        prior = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        try:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = keychain_token
            self._sdk = build_anthropic_sdk_client(
                provider=self._config.provider,
                bedrock_region=self._config.bedrock.aws_region,
            )
            # Drop any cached plan so the next call hits the fresh SDK.
            self._cache.clear()
            # If the breaker was OPEN due to prior auth failures, reset it
            # so the user gets an immediate retry.
            self._circuit.record_success()
            logger.info("Planner SDK rebuilt with refreshed Bedrock token")
            return True
        except Exception:
            logger.exception("Planner SDK rebuild failed")
            return False
        finally:
            if prior is None:
                os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
            else:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = prior

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

        # F20: hard kill-switch — once today's spend crosses the
        # configured ceiling, serve the deterministic fallback plan and
        # stamp the metadata so the dashboard banner can explain why.
        if (
            self._cost_tracker is not None
            and self._cost_tracker.check_budget() == "KILL"
        ):
            logger.error(
                "LLM daily budget exceeded; serving deterministic fallback "
                "(cid=%s)",
                get_correlation_id() or "-",
            )
            killed = build_fallback_plan(context)
            killed.metadata["fallback_reason"] = "budget_killed"
            killed.metadata["budget_killed"] = True
            return killed

        if not self._circuit.allow(now_mono):
            # F27: surface the fact that this plan came from the rule-
            # based fallback path, not the LLM. ``build_fallback_plan``
            # already stamps ``source=fallback``; we overwrite
            # ``fallback_reason`` with the specific cause so the overlay
            # / dashboard can present it. ``LLM_FALLBACK`` is emitted
            # so an aggregator can count breaker openings without
            # parsing the warning-text format.
            logger.warning(
                "LLM circuit open; serving deterministic fallback (cid=%s)",
                get_correlation_id() or "-",
            )
            logger.info(
                "%s reason=circuit_open cid=%s",
                EventType.LLM_FALLBACK.value,
                get_correlation_id() or "-",
            )
            fallback = build_fallback_plan(context)
            fallback.metadata["fallback_reason"] = "circuit_open"
            return fallback

        tier = self._select_tier(template_name)
        model_id = self._models[tier]
        # F29 (audit): scope a TruncationReport across the prompt-build
        # so we know which sections lost content. The report is stamped
        # onto ``InterventionPlan.metadata["context_truncated_sections"]``
        # after parse so the overlay can offer a "Show more context"
        # affordance.
        with capture_truncation_report() as _truncation_report:
            system_blocks, messages = build_anthropic_messages(
                context,
                state,
                constraints,
                template_name=template_name,
                extra_context=extra_context,
            )

        # F30: estimate the input-token cost before issuing the call so
        # the cancellation cost path can bill *something* if the response
        # never arrives. The Anthropic SDK does not echo back the
        # request tokens on cancellation, so we approximate with a
        # chars/4 heuristic over the assembled prompt — same heuristic
        # ``prompts._estimate_tokens`` uses internally.
        estimated_input_tokens = _estimate_request_input_tokens(
            system_blocks, messages,
        )

        attempts = 3
        for attempt in range(attempts):
            # audit-w2: re-consult the daily cost ceiling on every retry,
            # not just on the first attempt. A successful but token-heavy
            # response on attempt 1 (or a partial response billed via the
            # cancellation path in F30) can push the day's spend over
            # ``BUDGET_KILL`` mid-call. Without this re-check the retry
            # loop happily burns another two attempts past the ceiling.
            if (
                attempt > 0
                and self._cost_tracker is not None
                and self._cost_tracker.check_budget() == "KILL"
            ):
                logger.error(
                    "LLM daily budget exceeded mid-retry; serving "
                    "deterministic fallback (cid=%s, attempt=%d)",
                    get_correlation_id() or "-",
                    attempt + 1,
                )
                killed = build_fallback_plan(context)
                killed.metadata["fallback_reason"] = "budget_killed"
                killed.metadata["budget_killed"] = True
                killed.metadata["budget_killed_on_retry"] = attempt + 1
                return killed
            t0 = time.perf_counter()
            response: Any = None
            try:
                async with self._semaphore:
                    # swift-concurrency-pro rule (transferred to asyncio):
                    # shield the Bedrock call from cooperative cancellation.
                    # If the caller cancels mid-flight (state pipeline
                    # tear-down, daemon SIGTERM), we still let the SDK
                    # finish its current HTTP transaction cleanly so the
                    # Bedrock connection isn't left in a half-open state.
                    # F30: catch CancelledError so we still record the
                    # cost — the shielded call kept billing tokens even
                    # though the caller stopped waiting.
                    try:
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
                    except asyncio.CancelledError:
                        # The SDK call may have completed before the
                        # cancellation propagated. Record cost from the
                        # response if available; otherwise bill the
                        # best-estimate input tokens with ``output=0``.
                        self._record_cost_on_cancellation(
                            model_id,
                            response,
                            estimated_input_tokens,
                        )
                        raise
            except (RateLimitError, APITimeoutError, APIStatusError) as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                # Audit-2 fix: surface 401 / 403 (revoked or invalid
                # BYOK token) as a distinct, non-retryable failure so the
                # user gets an immediate signal that their token is bad.
                # The plain backoff path used to retry 401s up to
                # ``attempts`` times, then silently fall back to the
                # rule-based plan — the user never knew their token had
                # been revoked.
                status = getattr(exc, "status_code", None)
                if isinstance(exc, APIStatusError) and status in (401, 403):
                    logger.error(
                        "llm.request status=auth_error model=%s template=%s "
                        "latency_ms=%.0f http=%s err=%s — token may be revoked",
                        model_id,
                        template_name,
                        latency_ms,
                        status,
                        type(exc).__name__,
                    )
                    self._circuit.record_failure(time.monotonic())
                    auth_fallback = build_fallback_plan(context)
                    auth_fallback.metadata["fallback_reason"] = "auth_error"
                    auth_fallback.metadata["source"] = "fallback"
                    auth_fallback.metadata["http_status"] = status
                    return auth_fallback
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
                # Bounded exponential backoff with jitter. Wrap in
                # try/finally so a cancellation during the sleep still
                # records best-effort cost telemetry for the prior
                # attempt's billed call (audit-2 fix for F30 retry-loop
                # gap).
                try:
                    await asyncio.sleep(min(2 ** attempt + random.random(), 8.0))
                except asyncio.CancelledError:
                    self._record_cost_on_cancellation(
                        model_id,
                        response,
                        estimated_input_tokens,
                    )
                    raise
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
            # F19: include the active correlation id so downstream cost
            # accounting (F20) can group spend by originating request.
            logger.info(
                "llm.request status=ok model=%s template=%s tier=%s "
                "latency_ms=%.0f tokens_in=%s tokens_out=%s "
                "cache_read=%s cache_write=%s cid=%s",
                model_id,
                template_name,
                tier,
                latency_ms,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
                get_correlation_id() or "-",
            )

            # F20: persist the per-call USD cost into the daily ledger
            # and emit ``LLM_COST``. Best-effort — never let an
            # accounting bug propagate up and break the planner result.
            self._record_cost(model_id, usage, cancelled=False)

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
            # F29 (audit): stamp truncated-section names on plan.metadata
            # so the overlay can render the "Show more context"
            # affordance. Only populated when at least one section
            # actually lost content — silent on the happy path.
            if _truncation_report.truncated:
                enriched.metadata["context_truncated_sections"] = list(
                    _truncation_report.sections_trimmed
                )
            self._cache.put(context, enriched, state, constraints)
            return enriched

        # All retries exhausted → deterministic fallback.
        # F27: stamp metadata so the overlay can surface "offline mode"
        # and so dismissal-model training can exclude fallback outcomes.
        logger.warning(
            "LLM call exhausted retries for template=%s; using fallback",
            template_name,
        )
        logger.info(
            "%s reason=retries_exhausted cid=%s",
            EventType.LLM_FALLBACK.value,
            get_correlation_id() or "-",
        )
        fallback = build_fallback_plan(context)
        fallback.metadata["fallback_reason"] = "retries_exhausted"
        return fallback

    # ------------------------------------------------------------------
    # F20: cost accounting helper
    # ------------------------------------------------------------------

    def _record_cost(
        self,
        model_id: str,
        usage: Any,
        *,
        cancelled: bool,
    ) -> None:
        """Persist the per-call USD cost into the daily ledger.

        Best-effort: surfaces an exception only if the ledger path is
        broken at the file-system level, in which case the tracker has
        already logged the failure.
        """
        if self._cost_tracker is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        try:
            usd = usd_cost(
                model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
            )
        except (KeyError, ValueError) as exc:
            logger.warning(
                "cost_tracker: skipped unknown model %s (%s)",
                model_id,
                exc,
            )
            return
        try:
            self._cost_tracker.record(
                get_correlation_id(),
                model_id,
                usd,
                cancelled=cancelled,
            )
        except Exception:  # noqa: BLE001 — telemetry must never break the planner
            logger.exception("cost_tracker.record failed")

    def _record_cost_on_cancellation(
        self,
        model_id: str,
        response: Any,
        estimated_input_tokens: int,
    ) -> None:
        """Cost path taken when the shielded SDK call was cancelled (F30).

        If the response arrived before cancellation propagated we have
        real ``usage`` numbers; otherwise we bill the request-side
        estimate with ``output_tokens=0`` so the day's spend at least
        reflects the tokens the request shipped. The ``cancelled=True``
        flag on the cost record lets the aggregator distinguish
        cancellation cost from successful spend.
        """
        if self._cost_tracker is None:
            return
        usage = (
            getattr(response, "usage", None) if response is not None else None
        )
        if usage is not None:
            # Response arrived — bill real numbers but tag cancelled.
            self._record_cost(model_id, usage, cancelled=True)
            return
        # Pre-response cancellation — bill the best estimate.
        try:
            usd = usd_cost(
                model_id,
                input_tokens=max(0, int(estimated_input_tokens)),
                output_tokens=0,
            )
        except (KeyError, ValueError) as exc:
            logger.warning(
                "cost_tracker: cancellation cost skipped for %s (%s)",
                model_id,
                exc,
            )
            return
        try:
            self._cost_tracker.record(
                get_correlation_id(),
                model_id,
                usd,
                cancelled=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("cost_tracker.record (cancellation) failed")

    async def health_check(self) -> bool:
        """Cheap readiness check — never crash the daemon if the SDK is down."""
        try:
            # No dedicated ping endpoint on Anthropic; just probe model resolution.
            _ = self._models["default"]
            return True
        except Exception:  # noqa: BLE001
            return False
