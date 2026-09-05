"""AnthropicPlanner unit tests — structured-output production LLM path.

These tests exercise the planner with a stub Anthropic SDK so they run
without network access or credentials. Covers:

* Structured-output request shape (no tools / tool_choice / temperature)
* Response parsing: first text block → PlanDraft → InterventionPlan
* Model-tier routing (fast / default / deep) and template coverage
* Retry on RateLimitError with bounded backoff
* Circuit breaker opens after consecutive failures, serves fallback
* Cache hit short-circuits the SDK call
* Provider resolution from logical model IDs
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import APIStatusError, RateLimitError

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.libs.llm.anthropic_client import (
    resolve_anthropic_model_id,
)
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
)
from cortex.services.llm_engine.anthropic_planner import (
    _TEMPLATE_TIER,
    AnthropicPlanner,
    _CircuitBreaker,
    parse_plan_response,
)
from cortex.services.llm_engine.prompts import PROMPT_TEMPLATES

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_context() -> TaskContext:
    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.6,
        editor_context=EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 40),
            symbol_at_cursor="handle_request",
            diagnostics=[],
            recent_edits=[],
        ),
    )


def _make_state() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.0, hyper=0.9, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9, overall=0.9,
        ),
        timestamp=100.0,
        dwell_seconds=35.0,
    )


_VALID_DRAFT: dict[str, Any] = {
    "situation_summary": "1 error in main.py",
    "primary_focus": "main.py:10",
    "headline": "Fix the NameError on line 10",
    "causal_explanation": "1 active error pulled focus off the function.",
    "micro_steps": ["Read the NameError", "Define x before use"],
    "hide_targets": ["editor_symbols_except_current_function"],
    "ui_plan": {
        "dim_background": False,
        "show_overlay": True,
        "fold_unrelated_code": True,
        "intervention_type": "overlay_only",
    },
    "tone": "supportive",
    "suggested_actions": [],
    "error_analysis": None,
    "tab_recommendations": None,
}


def _stub_response(
    payload: dict[str, Any] | None = None,
    *,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    """Build a fake Messages API response: one text block holding JSON."""
    text = json.dumps(payload if payload is not None else _VALID_DRAFT)
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _make_stub_sdk(
    response: Any | None = None,
    side_effect: Any = None,
) -> MagicMock:
    sdk = MagicMock()
    create_mock = AsyncMock(return_value=response or _stub_response())
    if side_effect is not None:
        create_mock = AsyncMock(side_effect=side_effect)
    sdk.messages = MagicMock()
    sdk.messages.create = create_mock
    return sdk


def _make_planner(**config_kwargs: Any) -> AnthropicPlanner:
    """Construct an AnthropicPlanner with a deterministic stub SDK."""
    sdk = config_kwargs.pop("_sdk", None) or _make_stub_sdk()
    cfg = LLMConfig(
        provider=config_kwargs.pop("provider", "bedrock"),
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
        **config_kwargs,
    )
    planner = AnthropicPlanner(cfg, sdk=sdk, _allow_unbrokered_test_requests=True)
    # Skip the real jittered backoff so retry tests stay fast.
    planner._backoff = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    return planner


# ---------------------------------------------------------------------------
# Model-ID resolution
# ---------------------------------------------------------------------------


def test_resolve_bedrock_mantle_id():
    assert (
        resolve_anthropic_model_id("claude-sonnet-5", provider="bedrock")
        == "anthropic.claude-sonnet-5"
    )


def test_resolve_vertex_id():
    assert resolve_anthropic_model_id("claude-opus-5", provider="vertex") == "claude-opus-5"
    assert (
        resolve_anthropic_model_id("claude-haiku-4-5", provider="vertex")
        == "claude-haiku-4-5@20251001"
    )


def test_resolve_direct_passthrough():
    assert (
        resolve_anthropic_model_id("claude-haiku-4-5", provider="direct")
        == "claude-haiku-4-5"
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_plan_response_reads_first_text_block():
    parsed = parse_plan_response(_stub_response())
    assert parsed.plan is not None
    assert parsed.failure_reason is None
    assert parsed.stop_reason == "end_turn"
    assert parsed.plan.headline == "Fix the NameError on line 10"


def test_parse_plan_response_without_text_block_is_invalid_and_retryable():
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="tool_use", name="x", input={})],
        usage=None,
    )
    parsed = parse_plan_response(response)
    assert parsed.plan is None
    assert parsed.failure_reason == "invalid_response"
    assert parsed.retryable is True


def test_parse_plan_response_rejects_non_json_and_non_object():
    for text in ("not json", "[1, 2]", "42"):
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=None,
        )
        assert parse_plan_response(response).failure_reason == "invalid_response"


# ---------------------------------------------------------------------------
# Successful round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_plan_success_round_trip():
    planner = _make_planner()
    plan = await planner.generate_intervention_plan(
        _make_context(),
        _make_state(),
        template_name="debug_error_summary",
    )
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "overlay_only"
    assert plan.metadata["source"] == "llm"
    planner._sdk.messages.create.assert_awaited_once()
    call_kwargs = planner._sdk.messages.create.await_args.kwargs
    # debug_error_summary → deep tier → Opus 5 on Bedrock Mantle.
    assert call_kwargs["model"] == "anthropic.claude-opus-5"
    assert call_kwargs["max_tokens"] == 8192
    assert call_kwargs["output_config"]["format"]["type"] == "json_schema"
    assert call_kwargs["output_config"]["effort"] == "medium"
    for forbidden in ("tools", "tool_choice", "temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in call_kwargs
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_template_tier_routes_to_fast_model_without_effort():
    planner = _make_planner()
    await planner.generate_intervention_plan(
        _make_context(),
        _make_state(),
        template_name="calm_overlay_writer",
    )
    call_kwargs = planner._sdk.messages.create.await_args.kwargs
    assert call_kwargs["model"] == "anthropic.claude-haiku-4-5"
    assert "effort" not in call_kwargs["output_config"]


@pytest.mark.asyncio
async def test_template_tier_override_wins():
    planner = _make_planner(template_tier_overrides={"calm_overlay_writer": "deep"})
    await planner.generate_intervention_plan(
        _make_context(),
        _make_state(),
        template_name="calm_overlay_writer",
    )
    call_kwargs = planner._sdk.messages.create.await_args.kwargs
    assert call_kwargs["model"] == "anthropic.claude-opus-5"


def test_template_tier_map_covers_exactly_the_prompt_templates():
    # D12: the map used to name three non-existent templates and omit five
    # real ones. Both key sets must be identical so no template silently
    # falls to the default tier.
    assert set(_TEMPLATE_TIER) == set(PROMPT_TEMPLATES)
    assert _TEMPLATE_TIER["deep_bottleneck_diagnosis"] == "deep"
    assert _TEMPLATE_TIER["recovery_reinforcer"] == "fast"
    assert _TEMPLATE_TIER["re_engage_planner"] == "default"


def test_model_for_template_uses_tier_routing():
    planner = _make_planner()
    assert planner.model_for_template("debug_error_summary") == "anthropic.claude-opus-5"
    assert planner.model_for_template("browser_tab_reduction") == "anthropic.claude-haiku-4-5"
    assert planner.model_for_template(None) == "anthropic.claude-sonnet-5"


# ---------------------------------------------------------------------------
# Retry + fallback behaviour
# ---------------------------------------------------------------------------


def _rate_limit_error() -> RateLimitError:
    return RateLimitError(
        "throttled",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )


@pytest.mark.asyncio
async def test_retries_on_rate_limit_then_succeeds():
    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=[_rate_limit_error(), _stub_response()])
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.level == "overlay_only"
    assert plan.metadata["source"] == "llm"
    assert sdk.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_exhausted_retries_return_fallback_plan():
    sdk = _make_stub_sdk(side_effect=_rate_limit_error())
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    # The deterministic fallback is always level=overlay_only and has a
    # supportive tone.
    assert plan.level == "overlay_only"
    assert plan.tone == "supportive"
    assert plan.metadata["fallback_reason"] == "retries_exhausted"
    assert sdk.messages.create.await_count == planner._config.planner_attempts == 3


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_and_recovers():
    cb = _CircuitBreaker(threshold=2, window_seconds=60.0, open_seconds=10.0)
    assert cb.allow(now=0.0)
    cb.record_failure(now=1.0)
    cb.record_failure(now=2.0)
    assert not cb.allow(now=3.0)        # open
    assert not cb.allow(now=11.0)       # still inside cool-down
    assert cb.allow(now=12.5)           # half-open after open_seconds
    cb.record_success()
    assert cb.allow(now=100.0)


def test_circuit_breaker_trip_opens_immediately():
    cb = _CircuitBreaker(threshold=5, window_seconds=60.0, open_seconds=10.0)
    cb.trip(now=1.0)
    assert cb.is_open
    assert not cb.allow(now=2.0)
    assert cb.allow(now=11.5)


@pytest.mark.asyncio
async def test_open_circuit_serves_fallback_without_calling_sdk():
    import time as _time

    sdk = _make_stub_sdk()
    planner = _make_planner(_sdk=sdk)
    # Trip the breaker so it's open right NOW (monotonic clock is offset
    # from epoch — using ``1.0`` would put the open time millions of
    # seconds in the past and the breaker would auto-recover).
    planner._circuit._opened_at = _time.monotonic()  # noqa: SLF001
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.level == "overlay_only"
    assert plan.metadata["fallback_reason"] == "circuit_open"
    sdk.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_breakers_are_per_tier():
    import time as _time

    sdk = _make_stub_sdk()
    planner = _make_planner(_sdk=sdk)
    planner._circuits["deep"]._opened_at = _time.monotonic()  # noqa: SLF001
    deep = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="debug_error_summary",
    )
    assert deep.metadata["fallback_reason"] == "circuit_open"
    assert deep.metadata["tier"] == "deep"
    sdk.messages.create.assert_not_awaited()
    fast = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="calm_overlay_writer",
    )
    assert fast.metadata["source"] == "llm"
    sdk.messages.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_short_circuits_sdk_call():
    sdk = _make_stub_sdk()
    planner = _make_planner(_sdk=sdk)
    ctx = _make_context()
    state = _make_state()
    await planner.generate_intervention_plan(
        ctx, state, template_name="micro_step_planner",
    )
    await planner.generate_intervention_plan(
        ctx, state, template_name="micro_step_planner",
    )
    # Second call must hit the cache, not the SDK.
    assert sdk.messages.create.await_count == 1


# ---------------------------------------------------------------------------
# Invalid payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_payload_triggers_retry_then_fallback():
    bad = _stub_response(payload={"this": "is not a plan"})
    sdk = _make_stub_sdk(response=bad)
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    # Invalid payloads exhaust retries and fall back to the deterministic
    # plan, which is always level=overlay_only.
    assert plan.level == "overlay_only"
    assert plan.metadata["fallback_reason"] == "invalid_response"
    assert sdk.messages.create.await_count == 3


# ---------------------------------------------------------------------------
# Non-retryable API errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_request_is_not_retried():
    fatal = APIStatusError(
        "bad request",
        response=MagicMock(status_code=400, headers={}),
        body=None,
    )
    sdk = _make_stub_sdk(side_effect=fatal)
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.level == "overlay_only"
    assert plan.metadata["fallback_reason"] == "bad_request"
    assert plan.metadata["http_status"] == 400
    assert sdk.messages.create.await_count == 1


def test_worst_case_seconds_matches_config():
    planner = _make_planner()
    assert planner.worst_case_seconds == planner._config.planner_worst_case_seconds
    # 3 attempts × 2 s per-attempt timeout + capped backoff (2 s + 3 s).
    assert planner.worst_case_seconds == pytest.approx(3 * 2.0 + 5.0)
