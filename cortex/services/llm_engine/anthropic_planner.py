"""Anthropic SDK intervention planner — production LLM path.

Single concrete implementation of the ``LLMClient`` Protocol. Uses
``AsyncAnthropicBedrockMantle`` as the production transport with
``AsyncAnthropic`` / ``AsyncAnthropicVertex`` as drop-in escape hatches
selected by ``LLMConfig.provider``.

Design notes
------------
* **Structured outputs.** Every request carries
  ``output_config={"format": {"type": "json_schema", "schema": ...}}`` built
  from :class:`~cortex.services.llm_engine.plan_draft.PlanDraft`, the only
  shape the model may author. The first ``text`` block of the response is
  JSON valid against that schema; no tools, no forced ``tool_choice``, no
  sampling parameters (current models reject ``temperature`` with 400).
* **Per-template model tier.** Latency-critical short outputs go to the
  fast tier (Haiku); standard planning to the default tier (Sonnet);
  multi-step debugging to the deep tier (Opus). ``_TEMPLATE_TIER`` covers
  every ``PROMPT_TEMPLATES`` key; ``LLMConfig.template_tier_overrides``
  wins per template.
* **Prompt caching.** The system prompt is marked ``cache_control:
  ephemeral`` so back-to-back interventions reuse it (subject to each
  model's minimum cacheable prefix; see ``MODEL_CAPABILITIES``).
* **Resilience.** The SDK client is built with ``max_retries=0`` so this
  module owns the only retry policy: retry ``RateLimitError`` /
  ``APITimeoutError`` / ``APIConnectionError`` and HTTP 408/409/429/5xx
  with bounded jittered backoff; 401/403 → ``auth_error``, 400/422 →
  ``bad_request``, 404 → ``model_unavailable`` (non-retryable, trip the
  tier's breaker immediately); ``stop_reason`` ``refusal`` /
  ``max_tokens`` → non-retryable fallbacks. One circuit breaker per tier
  so a broken deep-tier model id cannot black out fast-tier calls. A
  bounded semaphore caps in-flight concurrency.
* **Cancellation.** The SDK call runs as its own task behind
  ``asyncio.shield``. If the caller is cancelled mid-flight the task keeps
  running to completion (bounded by the SDK timeout); a done-callback
  records its real usage/cost (or failure) and releases the semaphore, so
  orphaned calls never leak slots or spend.
* **Observability.** Each call emits a structured ``llm.request`` log
  event (model id, template, tier, latency, cache hit/write, stop reason).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from pydantic import ValidationError

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds

# audit Phase-I: ``keyring`` is imported lazily inside
# :func:`_keychain_get_bedrock_token`. The module is heavyweight
# (~80 ms cold import on macOS — it scans the keyring backend entry
# points via ``importlib.metadata``) and we only need it the first
# time the planner is asked to mint a Bedrock client. Deferring the
# import shaves measurable time off daemon startup. The regression
# guard lives in ``cortex/tests/performance/test_startup_latency.py``.
from cortex.libs.config.settings import LLMConfig
from cortex.libs.llm.anthropic_client import (
    EffortLevel,
    build_anthropic_sdk_client,
    model_capabilities_or_conservative,
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
from cortex.libs.schemas.privacy import ContextFieldDisclosure
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.utils.platform import get_config_dir
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import build_fallback_plan
from cortex.services.llm_engine.cost_tracker import CostTracker
from cortex.services.llm_engine.parser import (
    enrich_plan_with_context,
    validate_intervention_plan,
)
from cortex.services.llm_engine.plan_draft import (
    PlanDraft,
    draft_to_plan_data,
    structured_output_schema,
)
from cortex.services.llm_engine.prompts import (
    build_anthropic_messages,
    capture_truncation_report,
)

logger = logging.getLogger(__name__)

ModelTier = Literal["fast", "default", "deep"]
_TIERS: tuple[ModelTier, ...] = ("fast", "default", "deep")

# Map every Cortex prompt template to a model tier. Latency-critical
# short outputs use the fast tier; multi-step causal reasoning the deep
# tier. ``test_anthropic_planner.py`` asserts this covers exactly the
# ``PROMPT_TEMPLATES`` key set.
_TEMPLATE_TIER: dict[str, ModelTier] = {
    # fast — short overlay copy, tab triage, ambient nudges
    "calm_overlay_writer": "fast",
    "browser_tab_reduction": "fast",
    "breathing_overlay": "fast",
    "pre_break_warning": "fast",
    "recovery_reinforcer": "fast",
    # default — standard planning
    "micro_step_planner": "default",
    "code_focus_reduction": "default",
    "active_recall": "default",
    "rabbit_hole": "default",
    "alignment_summary": "default",
    "re_engage_planner": "default",
    # deep — multi-step debugging diagnosis
    "debug_error_summary": "deep",
    "deep_bottleneck_diagnosis": "deep",
}

# HTTP statuses (besides 5xx) worth a retry: request timeout, conflict,
# rate limit. Everything else in 4xx is a request/config problem that a
# retry cannot fix.
_RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({408, 409, 429})


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a planner instance)
# ---------------------------------------------------------------------------


def build_request_kwargs(
    *,
    model_id: str,
    max_tokens: int,
    effort: EffortLevel | None,
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    timeout_seconds: float,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the ``messages.create`` keyword arguments for one call.

    * ``output_config.format`` always carries the :class:`PlanDraft`
      structured-output schema (no ``tools`` / ``tool_choice``).
    * ``output_config.effort`` is included only for models that accept it
      (Opus 4.7/5, Sonnet 5, Sonnet 4.6); Haiku 4.5 rejects it.
    * No sampling parameters are ever sent — ``temperature`` / ``top_p`` /
      ``top_k`` return HTTP 400 on Opus 4.7/5 and Sonnet 5.
    * ``thinking`` is omitted: Opus 5 / Sonnet 5 run adaptive thinking by
      default and ``max_tokens`` caps thinking plus text.
    """
    capabilities = model_capabilities_or_conservative(model_id)
    schema = output_schema if output_schema is not None else structured_output_schema()
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": schema},
    }
    if effort is not None and capabilities.supports_effort:
        output_config["effort"] = effort
    return {
        "model": model_id,
        "max_tokens": int(max_tokens),
        "system": system_blocks,
        "messages": messages,
        "output_config": output_config,
        "timeout": float(timeout_seconds),
    }


@dataclass(frozen=True, slots=True)
class ParsedPlanResponse:
    """Outcome of turning one HTTP response into an :class:`InterventionPlan`.

    ``plan`` is set on success. Otherwise ``failure_reason`` is one of
    ``refusal`` / ``max_tokens_truncated`` / ``context_window_exceeded``
    (non-retryable — the same prompt would fail again) or
    ``invalid_response`` (retryable — transient model glitch).
    """

    plan: InterventionPlan | None
    draft: PlanDraft | None
    stop_reason: str | None
    failure_reason: str | None
    retryable: bool
    detail: str = ""


def _first_text_block(response: Any) -> str | None:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                return text
    return None


def parse_plan_response(response: Any) -> ParsedPlanResponse:
    """Validate a Messages API response into a plan (never raises).

    ``stop_reason`` is inspected first: ``refusal`` (HTTP 200 from the
    safety classifiers on Opus 5 / Sonnet 5) and ``max_tokens`` (truncated
    JSON) are terminal for this request. Otherwise the first ``text``
    block is parsed as JSON, validated as a :class:`PlanDraft` (which
    forbids every daemon-owned field), rejected when degenerate (empty
    headline / summary / steps), and finally normalised into an
    :class:`InterventionPlan`.
    """
    raw_stop = getattr(response, "stop_reason", None)
    stop_reason = raw_stop if isinstance(raw_stop, str) else None
    if stop_reason == "refusal":
        return ParsedPlanResponse(None, None, stop_reason, "refusal", False, "model refused")
    if stop_reason == "max_tokens":
        return ParsedPlanResponse(
            None, None, stop_reason, "max_tokens_truncated", False, "output hit max_tokens"
        )
    if stop_reason == "model_context_window_exceeded":
        return ParsedPlanResponse(
            None, None, stop_reason, "context_window_exceeded", False, "context window exceeded"
        )

    text = _first_text_block(response)
    if text is None:
        return ParsedPlanResponse(
            None, None, stop_reason, "invalid_response", True, "no text block in response"
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        return ParsedPlanResponse(
            None, None, stop_reason, "invalid_response", True, f"JSON decode failed: {exc}"
        )
    if not isinstance(data, dict):
        return ParsedPlanResponse(
            None, None, stop_reason, "invalid_response", True, "JSON payload is not an object"
        )
    try:
        draft = PlanDraft.model_validate(data)
    except ValidationError as exc:
        return ParsedPlanResponse(
            None,
            None,
            stop_reason,
            "invalid_response",
            True,
            f"PlanDraft validation failed ({exc.error_count()} errors)",
        )
    if draft.is_degenerate():
        return ParsedPlanResponse(
            None, draft, stop_reason, "invalid_response", True, "degenerate draft (empty plan)"
        )
    plan = validate_intervention_plan(draft_to_plan_data(draft))
    if plan is None:
        return ParsedPlanResponse(
            None, draft, stop_reason, "invalid_response", True, "InterventionPlan validation failed"
        )
    return ParsedPlanResponse(plan, draft, stop_reason, None, False)


@dataclass(frozen=True, slots=True)
class _ErrorDecision:
    retryable: bool
    reason: str  # ``retry`` or the fallback reason
    http_status: int | None


def classify_api_error(exc: BaseException) -> _ErrorDecision:
    """Map an SDK exception to retry / distinct non-retryable fallback.

    The ``isinstance`` checks read the module globals at call time so a
    test can substitute the SDK exception classes.
    """
    status_raw = getattr(exc, "status_code", None)
    status = status_raw if isinstance(status_raw, int) and not isinstance(status_raw, bool) else None
    if isinstance(exc, APIStatusError) or status is not None:
        if status in (401, 403):
            return _ErrorDecision(False, "auth_error", status)
        if status == 404:
            return _ErrorDecision(False, "model_unavailable", status)
        if status in (400, 422):
            return _ErrorDecision(False, "bad_request", status)
        if status is not None and (status in _RETRYABLE_HTTP_STATUS or status >= 500):
            return _ErrorDecision(True, "retry", status)
        if status is not None:
            # Any other 4xx (413 payload too large, ...) is a request problem.
            return _ErrorDecision(False, "bad_request", status)
    if isinstance(exc, RateLimitError | APITimeoutError | APIConnectionError):
        return _ErrorDecision(True, "retry", status)
    return _ErrorDecision(False, "api_error", status)


def _estimate_request_input_tokens(
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> int:
    """Best-effort input-token estimate for the assembled request.

    Used by the orphaned-call cost path: if a shielded call never
    completes (event-loop teardown), ``response.usage`` is unavailable,
    so we approximate from the request payload. The chars/4 heuristic
    matches :func:`cortex.services.llm_engine.prompts._estimate_tokens`
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

    Returns ``None`` when keyring is unavailable or no entry exists, in
    which case the transport factory falls back to the
    ``AWS_BEARER_TOKEN_BEDROCK`` environment variable.

    audit Phase-I: ``keyring`` is imported lazily here (rather than at
    module top) so importing :mod:`anthropic_planner` does not drag the
    keyring backend discovery into daemon startup. The keychain lookup
    only happens when the planner actually mints an LLM client.
    """
    if not config.use_keychain or config.provider != "bedrock":
        return None
    try:
        # Phase-4a Debt-1: route through ``get_password_safe`` so a
        # wedged macOS Keychain prompt cannot pin the planner-init
        # call for tens of seconds. The helper enforces a 5 s wall-
        # clock ceiling and returns ``None`` on timeout.
        from cortex.libs.utils.secrets import get_password_safe  # noqa: PLC0415

        return get_password_safe(
            config.bedrock.keychain_service,
            config.bedrock.keychain_account,
        )
    except Exception:  # noqa: BLE001 — keyring backend missing on Linux/Windows
        return None


class PlannerResult:
    """B11 (Phase 4.1): discriminated result of a planner call.

    The planner historically returned a single :class:`InterventionPlan`
    where the failure mode (timeout, parse error, budget kill, retry
    exhaustion, etc.) was inferable only by reading
    ``plan.metadata["fallback_reason"]`` — a stringly-typed field that
    callers had to know to inspect. This struct makes the failure mode
    a first-class discriminator so the route (and any future caller)
    can branch on it without parsing metadata.

    ``failure_mode`` literal values:
      * ``"ok"`` — live LLM call succeeded, no degradation.
      * ``"timeout"`` — call exhausted retries or the SDK timed out.
      * ``"parse_error"`` — response was unusable (invalid JSON / draft,
        truncated by ``max_tokens``, context window exceeded).
      * ``"empty_response"`` — fallback used because of an upstream
        constraint (budget kill, circuit open, auth error, bad request,
        model unavailable, refusal).
      * ``"cache_hit"`` — served from the in-process plan cache.

    The plan field is always populated (the daemon never returns no
    plan; the fallback path produces a deterministic plan) so callers
    can use the same downstream path regardless of failure_mode.
    """

    __slots__ = ("plan", "failure_mode")

    def __init__(self, plan: Any, failure_mode: str) -> None:
        self.plan = plan
        self.failure_mode = failure_mode

    def __repr__(self) -> str:
        return f"PlannerResult(failure_mode={self.failure_mode!r})"


_EMPTY_RESPONSE_REASONS: frozenset[str] = frozenset(
    {
        "budget_killed",
        "circuit_open",
        "auth_error",
        "bad_request",
        "model_unavailable",
        "api_error",
        "refusal",
    }
)
_PARSE_ERROR_REASONS: frozenset[str] = frozenset(
    {"invalid_response", "max_tokens_truncated", "context_window_exceeded"}
)


def classify_plan_failure_mode(plan: Any) -> str:
    """B11 (Phase 4.1): map an :class:`InterventionPlan` to a failure_mode.

    Inspects the plan's ``metadata.fallback_reason`` /
    ``metadata.source`` fields to derive the discriminator without
    requiring the caller to know which metadata keys mean what.
    """
    meta = getattr(plan, "metadata", None) or {}
    reason = str(meta.get("fallback_reason") or "")
    source = str(meta.get("source") or "")
    if reason in _EMPTY_RESPONSE_REASONS:
        return "empty_response"
    if reason == "retries_exhausted":
        return "timeout"
    if reason in _PARSE_ERROR_REASONS:
        return "parse_error"
    if source == "fallback":
        return "empty_response"
    return "ok"


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

    def trip(self, now: float) -> None:
        """Open immediately (deterministic request/config failure)."""
        self._failures.append(now)
        self._opened_at = now
        logger.warning("Anthropic circuit tripped open for %.0fs", self._open_seconds)

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


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
        clock: Clock | None = None,
        _allow_unbrokered_test_requests: bool = False,
    ) -> None:
        self._config = config or LLMConfig()
        self._clock = clock or SYSTEM_CLOCK
        self._allow_unbrokered_test_requests = _allow_unbrokered_test_requests

        # The Bedrock bearer token is passed to the SDK constructor
        # explicitly (see ``_build_sdk``); ``os.environ`` is never mutated,
        # so no child process (capture worker, native host, launcher
        # terminals) can inherit the credential.
        self._sdk: Any = sdk if sdk is not None else self._build_sdk()

        # Resolve each tier's provider-specific model identifier once.
        # ``model_fast`` / ``model_default`` / ``model_deep`` are typed as
        # ``LogicalModelId`` so the resolver tables stay in lock-step.
        self._logical_models: dict[ModelTier, str] = {
            "fast": self._config.model_fast,
            "default": self._config.model_default,
            "deep": self._config.model_deep,
        }
        self._models: dict[ModelTier, str] = {
            "fast": resolve_anthropic_model_id(
                self._config.model_fast,
                provider=self._config.provider,
            ),
            "default": resolve_anthropic_model_id(
                self._config.model_default,
                provider=self._config.provider,
            ),
            "deep": resolve_anthropic_model_id(
                self._config.model_deep,
                provider=self._config.provider,
            ),
        }

        self._cache = cache or LLMCache(
            default_ttl=self._config.cache_ttl_seconds,
            clock=self._clock,
        )
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_requests)
        # One breaker per tier: five 400s from a misconfigured deep-tier
        # model id must not black out fast-tier overlay copy.
        self._circuits: dict[ModelTier, _CircuitBreaker] = {
            tier: _CircuitBreaker(
                threshold=self._config.circuit_failure_threshold,
                window_seconds=self._config.circuit_window_seconds,
                open_seconds=self._config.circuit_open_seconds,
            )
            for tier in _TIERS
        }
        # Historical single-breaker attribute: aliases the default tier.
        self._circuit = self._circuits["default"]
        # Compiled once; the API caches the compiled grammar for 24 h.
        self._output_schema = structured_output_schema()

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
                    clock=self._clock,
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

    # ------------------------------------------------------------------
    # Transport construction / credential reload
    # ------------------------------------------------------------------

    def _bedrock_token(self, *, prefer_env: bool) -> str | None:
        """Resolve the Bedrock bearer token without touching ``os.environ``.

        Construction prefers an operator-supplied environment token and
        consults the Keychain only when the env is empty; a hot reload
        after the BYOK step reads the Keychain only (the env cannot have
        changed underneath a running daemon).
        """
        if self._config.provider != "bedrock":
            return None
        env_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK") if prefer_env else None
        if env_token:
            return env_token
        return _keychain_get_bedrock_token(self._config)

    def _build_sdk(self) -> Any:
        return build_anthropic_sdk_client(
            provider=self._config.provider,
            bedrock_region=self._config.bedrock.aws_region,
            bedrock_bearer_token=self._bedrock_token(prefer_env=True),
        )

    def reload_credentials(self) -> bool:
        """Rebuild the SDK client using the latest BYOK token.

        Audit-2 fix: previously the keychain-sourced Bedrock token was
        read once at planner construction. After the onboarding "save
        token" step or a Settings "rotate token" action, the running
        planner kept using the prior cached SDK client, so the very next
        intervention silently fell through to the rule-based fallback
        even though the user had just supplied a valid token.

        The token is passed explicitly to the transport factory — no
        ``os.environ`` mutation anywhere.

        Returns True if a working SDK client was constructed, False
        if no token is available or the rebuild raised. Callers can
        surface a UI toast on failure.
        """
        token: str | None = None
        if self._config.provider == "bedrock":
            token = self._bedrock_token(prefer_env=False)
            if not token:
                logger.warning("reload_credentials: no Bedrock token in keychain")
                return False
        try:
            self._sdk = build_anthropic_sdk_client(
                provider=self._config.provider,
                bedrock_region=self._config.bedrock.aws_region,
                bedrock_bearer_token=token,
            )
        except Exception:
            logger.exception("Planner SDK rebuild failed")
            return False
        # Drop any cached plan so the next call hits the fresh SDK.
        self._cache.clear()
        # If a breaker was OPEN due to prior auth failures, reset it so
        # the user gets an immediate retry.
        for breaker in self._circuits.values():
            breaker.record_success()
        logger.info("Planner SDK rebuilt for provider=%s", self._config.provider)
        return True

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def _select_tier(self, template_name: str | None) -> ModelTier:
        if template_name:
            overrides = self._config.template_tier_overrides
            if template_name in overrides:
                return overrides[template_name]
            if template_name in _TEMPLATE_TIER:
                return _TEMPLATE_TIER[template_name]
        return "default"

    def model_for_template(self, template_name: str | None) -> str:
        """Return the exact provider model id a request would use."""

        return self._models[self._select_tier(template_name)]

    @property
    def worst_case_seconds(self) -> float:
        """Upper bound on one ``generate_intervention_plan`` wall-clock time."""

        return self._config.planner_worst_case_seconds

    def _fallback(
        self,
        context: TaskContext,
        reason: str,
        *,
        tier: ModelTier | None = None,
        model_id: str | None = None,
        **extra: Any,
    ) -> InterventionPlan:
        """Deterministic plan stamped with a specific ``fallback_reason``.

        ``build_fallback_plan`` already stamps ``source=fallback``; the
        specific cause lets the overlay / dashboard explain "offline
        mode" and the dismissal model skip the outcome (F27).
        """
        logger.info(
            "%s reason=%s tier=%s model=%s cid=%s",
            EventType.LLM_FALLBACK.value,
            reason,
            tier,
            model_id,
            get_correlation_id() or "-",
        )
        plan = build_fallback_plan(context)
        plan.metadata["fallback_reason"] = reason
        if tier is not None:
            plan.metadata["tier"] = tier
        if model_id is not None:
            plan.metadata["model"] = model_id
        plan.metadata.update(extra)
        return plan

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
        disclosure_manifest: tuple[ContextFieldDisclosure, ...] | None = None,
        privacy_preview_id: str | None = None,
        privacy_confirmation: str | None = None,
    ) -> InterventionPlan:
        """Generate a typed intervention plan, with cache + retry + fallback."""
        # Authorization is owned by PrivacyAwarePlanner. These arguments are
        # accepted only for Protocol compatibility; callers must never compose
        # this transport primitive directly in production.
        del privacy_preview_id, privacy_confirmation
        if not disclosure_manifest and not self._allow_unbrokered_test_requests:
            logger.error(
                "Blocked unbrokered external planner request (cid=%s)",
                get_correlation_id() or "-",
            )
            blocked = build_fallback_plan(context)
            blocked.metadata["fallback_reason"] = "context_disclosure_manifest_required"
            blocked.metadata["network_used"] = False
            return blocked
        now_mono = monotonic_seconds(self._clock)

        # Cache hit short-circuits everything.
        cached = self._cache.get(
            context,
            state,
            constraints,
            template_name=template_name,
            extra_context=extra_context,
            disclosure_manifest=disclosure_manifest,
            now=now_mono,
        )
        if cached is not None:
            logger.debug("LLM cache hit (template=%s)", template_name)
            return cached

        # F20: hard kill-switch — once today's spend crosses the
        # configured ceiling, serve the deterministic fallback plan and
        # stamp the metadata so the dashboard banner can explain why.
        if self._cost_tracker is not None and self._cost_tracker.check_budget() == "KILL":
            logger.error(
                "LLM daily budget exceeded; serving deterministic fallback (cid=%s)",
                get_correlation_id() or "-",
            )
            return self._fallback(context, "budget_killed", budget_killed=True)

        tier = self._select_tier(template_name)
        model_id = self._models[tier]
        breaker = self._circuits[tier]
        if not breaker.allow(now_mono):
            logger.warning(
                "LLM circuit open for tier=%s; serving deterministic fallback (cid=%s)",
                tier,
                get_correlation_id() or "-",
            )
            return self._fallback(context, "circuit_open", tier=tier, model_id=model_id)

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
                disclosure_manifest=disclosure_manifest,
            )

        # Request-side token estimate for the orphaned-call cost path
        # (a shielded call cancelled at event-loop teardown never yields
        # ``usage``). Same chars/4 heuristic as ``prompts._estimate_tokens``.
        estimated_input_tokens = _estimate_request_input_tokens(system_blocks, messages)

        effort: EffortLevel = self._config.effort
        request_kwargs = build_request_kwargs(
            model_id=model_id,
            max_tokens=self._config.max_tokens,
            effort=effort,
            system_blocks=system_blocks,
            messages=messages,
            timeout_seconds=self._config.timeout_seconds,
            output_schema=self._output_schema,
        )
        effort_sent = "effort" in request_kwargs["output_config"]

        attempts = self._config.planner_attempts
        outcome = "retries_exhausted"
        outcome_extra: dict[str, Any] = {}
        for attempt in range(attempts):
            # audit-w2: re-consult the daily cost ceiling on every retry,
            # not just on the first attempt. A successful but token-heavy
            # response on attempt 1 can push the day's spend over
            # ``BUDGET_KILL`` mid-call; without this re-check the retry
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
                return self._fallback(
                    context,
                    "budget_killed",
                    budget_killed=True,
                    budget_killed_on_retry=attempt + 1,
                )
            t0 = monotonic_seconds(self._clock)
            try:
                response = await self._call_sdk(request_kwargs, model_id, estimated_input_tokens)
            except (RateLimitError, APITimeoutError, APIConnectionError, APIStatusError) as exc:
                latency_ms = (monotonic_seconds(self._clock) - t0) * 1000.0
                decision = classify_api_error(exc)
                now = monotonic_seconds(self._clock)
                if not decision.retryable:
                    # Audit-2 fix: surface 401 / 403 (revoked or invalid
                    # BYOK token) as a distinct, non-retryable failure so
                    # the user gets an immediate signal that their token is
                    # bad. 400/404/422 are request/config problems that a
                    # retry cannot fix: trip this tier's breaker at once
                    # instead of burning ``attempts`` calls per intervention.
                    logger.error(
                        "llm.request status=%s model=%s tier=%s template=%s "
                        "latency_ms=%.0f http=%s err=%s",
                        decision.reason,
                        model_id,
                        tier,
                        template_name,
                        latency_ms,
                        decision.http_status,
                        type(exc).__name__,
                    )
                    if decision.reason == "auth_error":
                        breaker.record_failure(now)
                    else:
                        breaker.trip(now)
                    return self._fallback(
                        context,
                        decision.reason,
                        tier=tier,
                        model_id=model_id,
                        http_status=decision.http_status,
                    )
                logger.warning(
                    "llm.request status=error model=%s tier=%s template=%s "
                    "latency_ms=%.0f attempt=%d http=%s err=%s",
                    model_id,
                    tier,
                    template_name,
                    latency_ms,
                    attempt + 1,
                    decision.http_status,
                    type(exc).__name__,
                )
                if attempt == attempts - 1:
                    breaker.record_failure(now)
                    outcome = "retries_exhausted"
                    break
                await self._backoff(attempt)
                continue
            except APIError as exc:
                latency_ms = (monotonic_seconds(self._clock) - t0) * 1000.0
                logger.error(
                    "llm.request status=fatal model=%s tier=%s template=%s "
                    "latency_ms=%.0f err=%s",
                    model_id,
                    tier,
                    template_name,
                    latency_ms,
                    type(exc).__name__,
                )
                breaker.record_failure(monotonic_seconds(self._clock))
                outcome = "api_error"
                break

            # The HTTP transaction completed: bill its real usage whatever
            # the parse outcome — the provider charged for it either way.
            latency_ms = (monotonic_seconds(self._clock) - t0) * 1000.0
            usage = getattr(response, "usage", None)
            self._record_cost(model_id, usage, cancelled=False)

            parsed = parse_plan_response(response)
            if parsed.plan is None:
                logger.warning(
                    "llm.request status=%s model=%s tier=%s template=%s "
                    "latency_ms=%.0f attempt=%d stop_reason=%s detail=%s",
                    parsed.failure_reason,
                    model_id,
                    tier,
                    template_name,
                    latency_ms,
                    attempt + 1,
                    parsed.stop_reason,
                    parsed.detail,
                )
                if not parsed.retryable:
                    breaker.record_failure(monotonic_seconds(self._clock))
                    outcome = parsed.failure_reason or "invalid_response"
                    outcome_extra = {"stop_reason": parsed.stop_reason}
                    break
                if attempt == attempts - 1:
                    breaker.record_failure(monotonic_seconds(self._clock))
                    outcome = "invalid_response"
                    outcome_extra = {"stop_reason": parsed.stop_reason}
                    break
                continue

            plan = parsed.plan
            breaker.record_success()
            # F19: include the active correlation id so downstream cost
            # accounting (F20) can group spend by originating request.
            logger.info(
                "llm.request status=ok model=%s template=%s tier=%s effort=%s "
                "latency_ms=%.0f tokens_in=%s tokens_out=%s "
                "cache_read=%s cache_write=%s stop_reason=%s cid=%s",
                model_id,
                template_name,
                tier,
                effort if effort_sent else None,
                latency_ms,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
                parsed.stop_reason,
                get_correlation_id() or "-",
            )

            # Daemon-owned provenance. ``PlanDraft`` cannot carry any of
            # these keys, so model output never reaches them.
            plan.metadata.update(
                {
                    "source": "llm",
                    "provider": self._config.provider,
                    "model": model_id,
                    "tier": tier,
                    "effort": effort if effort_sent else None,
                    "stop_reason": parsed.stop_reason,
                }
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
            # F29 (audit): stamp truncated-section names on plan.metadata
            # so the overlay can render the "Show more context"
            # affordance. Only populated when at least one section
            # actually lost content — silent on the happy path.
            if _truncation_report.truncated:
                enriched.metadata["context_truncated_sections"] = list(
                    _truncation_report.sections_trimmed
                )
            self._cache.put(
                context,
                enriched,
                state,
                constraints,
                template_name=template_name,
                extra_context=extra_context,
                disclosure_manifest=disclosure_manifest,
                now=monotonic_seconds(self._clock),
            )
            return enriched

        # Retries exhausted or a terminal response → deterministic fallback.
        # F27: stamp metadata so the overlay can surface "offline mode"
        # and so dismissal-model training can exclude fallback outcomes.
        logger.warning(
            "LLM call ended with %s for template=%s tier=%s; using fallback",
            outcome,
            template_name,
            tier,
        )
        return self._fallback(context, outcome, tier=tier, model_id=model_id, **outcome_extra)

    # ------------------------------------------------------------------
    # SDK call with cancellation-safe accounting
    # ------------------------------------------------------------------

    async def _backoff(self, attempt: int) -> None:
        """Bounded exponential backoff with jitter between attempts."""
        cap = self._config.planner_backoff_cap_seconds
        await asyncio.sleep(min(2**attempt + random.random(), cap))

    async def _call_sdk(
        self,
        request_kwargs: dict[str, Any],
        model_id: str,
        estimated_input_tokens: int,
    ) -> Any:
        """Run one ``messages.create`` behind the semaphore and a shield.

        The HTTP transaction runs as its own task so a cancelled caller
        (state-pipeline tear-down, daemon SIGTERM, the daemon's outer
        ``wait_for``) never leaves the connection half-open. When the
        caller *is* cancelled mid-flight, the task keeps running to
        completion (bounded by the SDK timeout, ``max_retries=0``) and a
        done-callback records its real usage / failure and releases the
        semaphore slot — nothing is orphaned.
        """
        await self._semaphore.acquire()
        task: asyncio.Future[Any] = asyncio.ensure_future(
            self._sdk.messages.create(**request_kwargs)
        )
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(
                functools.partial(
                    self._finalise_orphaned_call,
                    model_id=model_id,
                    estimated_input_tokens=estimated_input_tokens,
                )
            )
            raise
        except BaseException:
            self._semaphore.release()
            raise
        self._semaphore.release()
        return response

    def _finalise_orphaned_call(
        self,
        task: asyncio.Future[Any],
        *,
        model_id: str,
        estimated_input_tokens: int,
    ) -> None:
        """Done-callback for a call whose awaiting caller was cancelled."""
        try:
            if task.cancelled():
                # Never completed (event-loop teardown): bill the
                # request-side estimate with ``output_tokens=0``.
                logger.warning(
                    "llm.request status=orphan_cancelled model=%s cid=%s",
                    model_id,
                    get_correlation_id() or "-",
                )
                self._record_cost_on_cancellation(model_id, None, estimated_input_tokens)
                return
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "llm.request status=orphan_failed model=%s err=%s cid=%s",
                    model_id,
                    type(exc).__name__,
                    get_correlation_id() or "-",
                )
                return
            response = task.result()
            logger.info(
                "llm.request status=orphan_completed model=%s cid=%s",
                model_id,
                get_correlation_id() or "-",
            )
            self._record_cost_on_cancellation(model_id, response, estimated_input_tokens)
        except Exception:  # noqa: BLE001 — accounting must never raise inside a callback
            logger.exception("orphaned LLM call finalisation failed")
        finally:
            self._semaphore.release()

    # ------------------------------------------------------------------
    # F20: cost accounting helpers
    # ------------------------------------------------------------------

    def _record_cost(
        self,
        model_id: str,
        usage: Any,
        *,
        cancelled: bool,
    ) -> None:
        """Persist the per-call USD cost and token counts into the ledger.

        Best-effort: surfaces an exception only if the ledger path is
        broken at the file-system level, in which case the tracker has
        already logged the failure.
        """
        if self._cost_tracker is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
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
                prompt_tokens=input_tokens + cache_read + cache_write,
                completion_tokens=output_tokens,
            )
        except Exception:  # noqa: BLE001 — telemetry must never break the planner
            logger.exception("cost_tracker.record failed")

    def _record_cost_on_cancellation(
        self,
        model_id: str,
        response: Any,
        estimated_input_tokens: int,
    ) -> None:
        """Cost path for a call whose caller stopped waiting (F30).

        If the orphaned call completed we have real ``usage`` numbers;
        otherwise we bill the request-side estimate with
        ``output_tokens=0`` so the day's spend at least reflects the
        tokens the request shipped. The ``cancelled=True`` flag on the
        cost record lets the aggregator distinguish cancellation cost
        from successful spend.
        """
        if self._cost_tracker is None:
            return
        usage = getattr(response, "usage", None) if response is not None else None
        if usage is not None:
            # Response arrived — bill real numbers but tag cancelled.
            self._record_cost(model_id, usage, cancelled=True)
            return
        # Never completed — bill the best estimate.
        estimate = max(0, int(estimated_input_tokens))
        try:
            usd = usd_cost(model_id, input_tokens=estimate, output_tokens=0)
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
                prompt_tokens=estimate,
                completion_tokens=0,
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


__all__ = [
    "AnthropicPlanner",
    "ModelTier",
    "ParsedPlanResponse",
    "PlannerResult",
    "build_request_kwargs",
    "classify_api_error",
    "classify_plan_failure_mode",
    "parse_plan_response",
]
