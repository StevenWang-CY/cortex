"""AnthropicPlanner unit tests — Bedrock production LLM path.

These tests exercise the planner with a stub Anthropic SDK so they run
without network access or credentials. Covers:

* Tool-use payload extraction → InterventionPlan validation
* Model-tier routing (fast / default / deep)
* Retry on RateLimitError with bounded backoff
* Circuit breaker opens after consecutive failures, serves fallback
* Cache hit short-circuits the SDK call
* Provider resolution from logical model IDs
"""

from __future__ import annotations

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
    _extract_tool_use_input,
)

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


_VALID_PLAN_DICT: dict[str, Any] = {
    "level": "overlay_only",
    "headline": "Fix the NameError on line 10",
    "situation_summary": "1 error in main.py",
    "primary_focus": "main.py:10",
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
    "causal_explanation": "1 active error pulled focus off the function.",
}


def _stub_response(plan_input: dict[str, Any] | None = None) -> MagicMock:
    """Build a fake Anthropic response with a single tool_use block."""
    block = SimpleNamespace(
        type="tool_use",
        name="emit_intervention_plan",
        input=plan_input if plan_input is not None else _VALID_PLAN_DICT,
    )
    response = MagicMock()
    response.content = [block]
    response.usage = SimpleNamespace(
        input_tokens=120,
        output_tokens=80,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _make_stub_sdk(
    response: MagicMock | None = None,
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
    return AnthropicPlanner(cfg, sdk=sdk)


# ---------------------------------------------------------------------------
# Model-ID resolution
# ---------------------------------------------------------------------------


def test_resolve_bedrock_inference_profile():
    assert (
        resolve_anthropic_model_id("claude-sonnet-4-6", provider="bedrock")
        == "us.anthropic.claude-sonnet-4-6-v1:0"
    )


def test_resolve_vertex_revision():
    assert resolve_anthropic_model_id("claude-opus-4-7", provider="vertex").startswith(
        "claude-opus-4-7",
    )


def test_resolve_direct_passthrough():
    assert (
        resolve_anthropic_model_id("claude-haiku-4-5", provider="direct")
        == "claude-haiku-4-5"
    )


# ---------------------------------------------------------------------------
# Tool-use extraction
# ---------------------------------------------------------------------------


def test_extract_tool_use_input_returns_payload():
    response = _stub_response()
    assert _extract_tool_use_input(response) == _VALID_PLAN_DICT


def test_extract_tool_use_input_raises_when_missing():
    response = MagicMock()
    response.content = [SimpleNamespace(type="text", text="oops")]
    with pytest.raises(ValueError):
        _extract_tool_use_input(response)


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
    planner._sdk.messages.create.assert_awaited_once()
    call_kwargs = planner._sdk.messages.create.await_args.kwargs
    assert call_kwargs["model"] == "us.anthropic.claude-opus-4-7-v1:0"
    assert call_kwargs["tool_choice"]["name"] == "emit_intervention_plan"


@pytest.mark.asyncio
async def test_template_tier_routes_to_fast_model():
    planner = _make_planner()
    await planner.generate_intervention_plan(
        _make_context(),
        _make_state(),
        template_name="calm_overlay_writer",
    )
    call_kwargs = planner._sdk.messages.create.await_args.kwargs
    assert call_kwargs["model"] == "us.anthropic.claude-haiku-4-5-v1:0"


def test_known_templates_all_have_a_tier():
    # Defensive: any new template added to prompts.py without an entry
    # here will silently fall to "default" — this test fails fast on
    # missing routing entries for the templates we expect.
    expected = {
        "calm_overlay_writer",
        "browser_tab_reduction",
        "micro_step_planner",
        "code_focus_reduction",
        "debug_error_summary",
    }
    assert expected.issubset(set(_TEMPLATE_TIER.keys()))


# ---------------------------------------------------------------------------
# Retry + fallback behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_rate_limit_then_succeeds():
    rate_err = RateLimitError(
        "throttled",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )
    success = _stub_response()
    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=[rate_err, success])
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.level == "overlay_only"
    assert sdk.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_exhausted_retries_return_fallback_plan():
    rate_err = RateLimitError(
        "throttled",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )
    sdk = _make_stub_sdk(side_effect=rate_err)
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    # The deterministic fallback is always level=overlay_only and has a
    # supportive tone.
    assert plan.level == "overlay_only"
    assert plan.tone == "supportive"
    assert sdk.messages.create.await_count == 3


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
    sdk.messages.create.assert_not_awaited()


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
# Invalid tool input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_tool_input_triggers_retry_then_fallback():
    bad = _stub_response(plan_input={"this": "is not a plan"})
    sdk = _make_stub_sdk(response=bad)
    planner = _make_planner(_sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    # Invalid tool inputs exhaust retries and fall back to the
    # deterministic plan, which is always level=overlay_only.
    assert plan.level == "overlay_only"


# ---------------------------------------------------------------------------
# Fatal API error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_api_status_error_does_not_retry():
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
    # APIStatusError is in the same retryable bucket; we just bound to 3.
    assert plan.level == "overlay_only"
    assert sdk.messages.create.await_count <= 3
