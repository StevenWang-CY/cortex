"""Planner outcome matrix: stop reasons, HTTP statuses, metadata provenance.

Audit D1/D4/D5/D8/D9 regression coverage:

* ``refusal`` / ``max_tokens`` stop reasons are terminal (no retry) and
  carry distinct fallback reasons.
* 400/404/422 are non-retryable with distinct reasons and trip only the
  affected tier's breaker; 401/403 → ``auth_error``.
* 408/409/429/5xx, connection and timeout errors are retried.
* Successful plans are stamped with daemon-owned provenance; model output
  can never set daemon-owned fields; a degenerate ``{}``-style payload is
  rejected instead of becoming a placeholder plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.anthropic_planner import (
    AnthropicPlanner,
    classify_api_error,
    classify_plan_failure_mode,
)
from cortex.services.llm_engine.context_broker import PrivacyAwarePlanner
from cortex.services.llm_engine.cost_tracker import CostTracker


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
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
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


def _response(
    payload: dict[str, Any] | str | None = None,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    if payload is None:
        text = json.dumps(_VALID_DRAFT)
    elif isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload)
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _status_error(status: int) -> APIStatusError:
    return APIStatusError(
        f"http {status}",
        response=MagicMock(status_code=status, headers={}),
        body=None,
    )


def _planner(
    side_effect: Any,
    *,
    tracker: CostTracker | None = None,
) -> AnthropicPlanner:
    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=side_effect)
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    planner = AnthropicPlanner(
        cfg,
        sdk=sdk,
        cost_tracker=tracker,
        _allow_unbrokered_test_requests=True,
    )
    planner._backoff = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
    return planner


async def _run(planner: AnthropicPlanner, template: str = "micro_step_planner") -> Any:
    return await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name=template,
    )


# ---------------------------------------------------------------------------
# Terminal stop reasons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refusal_is_terminal_with_its_own_reason() -> None:
    planner = _planner([_response("", stop_reason="refusal")])
    plan = await _run(planner)
    assert plan.metadata["source"] == "fallback"
    assert plan.metadata["fallback_reason"] == "refusal"
    assert plan.metadata["stop_reason"] == "refusal"
    assert planner._sdk.messages.create.await_count == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_max_tokens_truncation_is_terminal_with_its_own_reason() -> None:
    truncated = json.dumps(_VALID_DRAFT)[:40]
    planner = _planner([_response(truncated, stop_reason="max_tokens")])
    plan = await _run(planner)
    assert plan.metadata["fallback_reason"] == "max_tokens_truncated"
    assert plan.metadata["stop_reason"] == "max_tokens"
    assert planner._sdk.messages.create.await_count == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_context_window_exceeded_is_terminal() -> None:
    planner = _planner([_response("", stop_reason="model_context_window_exceeded")])
    plan = await _run(planner)
    assert plan.metadata["fallback_reason"] == "context_window_exceeded"
    assert planner._sdk.messages.create.await_count == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# HTTP status handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(400, "bad_request"), (422, "bad_request"), (413, "bad_request"), (404, "model_unavailable")],
)
async def test_request_errors_are_not_retried_and_trip_the_tier_breaker(
    status: int, reason: str
) -> None:
    planner = _planner([_status_error(status), _response()])
    plan = await _run(planner, template="debug_error_summary")
    assert plan.metadata["fallback_reason"] == reason
    assert plan.metadata["http_status"] == status
    assert plan.metadata["tier"] == "deep"
    assert planner._sdk.messages.create.await_count == 1  # noqa: SLF001
    # Only the deep tier tripped: the next default-tier call still reaches the SDK.
    assert planner._circuits["deep"].is_open  # noqa: SLF001
    assert not planner._circuits["default"].is_open  # noqa: SLF001
    ok = await _run(planner, template="micro_step_planner")
    assert ok.metadata["source"] == "llm"
    assert planner._sdk.messages.create.await_count == 2  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_short_circuit(status: int) -> None:
    planner = _planner([_status_error(status)])
    plan = await _run(planner)
    assert plan.metadata["fallback_reason"] == "auth_error"
    assert plan.metadata["http_status"] == status
    assert planner._sdk.messages.create.await_count == 1  # noqa: SLF001
    # A single auth failure counts toward the breaker but does not trip it.
    assert not planner._circuits["default"].is_open  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 529])
async def test_transient_statuses_are_retried_then_succeed(status: int) -> None:
    planner = _planner([_status_error(status), _status_error(status), _response()])
    plan = await _run(planner)
    assert plan.metadata["source"] == "llm"
    assert planner._sdk.messages.create.await_count == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_persistent_5xx_exhausts_retries() -> None:
    planner = _planner([_status_error(503)] * 5)
    plan = await _run(planner)
    assert plan.metadata["fallback_reason"] == "retries_exhausted"
    assert planner._sdk.messages.create.await_count == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_connection_and_timeout_errors_are_retried() -> None:
    request = MagicMock()
    planner = _planner(
        [APIConnectionError(request=request), APITimeoutError(request=request), _response()]
    )
    plan = await _run(planner)
    assert plan.metadata["source"] == "llm"
    assert planner._sdk.messages.create.await_count == 3  # noqa: SLF001


def test_classify_api_error_matrix() -> None:
    request = MagicMock()
    assert classify_api_error(_status_error(429)).retryable is True
    assert classify_api_error(_status_error(500)).retryable is True
    assert classify_api_error(_status_error(408)).retryable is True
    assert classify_api_error(APITimeoutError(request=request)).retryable is True
    assert classify_api_error(APIConnectionError(request=request)).retryable is True
    for status, reason in ((400, "bad_request"), (404, "model_unavailable"), (401, "auth_error")):
        decision = classify_api_error(_status_error(status))
        assert decision.retryable is False
        assert decision.reason == reason
        assert decision.http_status == status
    unknown = classify_api_error(RuntimeError("boom"))
    assert unknown.retryable is False
    assert unknown.reason == "api_error"


# ---------------------------------------------------------------------------
# Provenance and daemon-owned fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_stamps_daemon_provenance() -> None:
    planner = _planner([_response()])
    plan = await _run(planner)
    assert plan.metadata["source"] == "llm"
    assert plan.metadata["provider"] == "bedrock"
    assert plan.metadata["model"] == "anthropic.claude-sonnet-5"
    assert plan.metadata["tier"] == "default"
    assert plan.metadata["effort"] == "medium"
    assert plan.metadata["stop_reason"] == "end_turn"
    assert plan.intervention_id.startswith("int_")
    assert plan.consent_level == "suggest"
    assert plan.trigger_url is None
    assert plan.causal_signals == []
    assert plan.plan_warnings == []


@pytest.mark.asyncio
async def test_effort_metadata_is_none_for_fast_tier() -> None:
    planner = _planner([_response()])
    plan = await _run(planner, template="calm_overlay_writer")
    assert plan.metadata["model"] == "anthropic.claude-haiku-4-5"
    assert plan.metadata["effort"] is None
    call_kwargs = planner._sdk.messages.create.await_args.kwargs  # noqa: SLF001
    assert "effort" not in call_kwargs["output_config"]


@pytest.mark.asyncio
async def test_model_output_cannot_set_daemon_owned_fields() -> None:
    hijack = {
        **_VALID_DRAFT,
        "intervention_id": "int_evil",
        "metadata": {"source": "llm", "fallback_reason": "none"},
        "consent_level": "autonomous_act",
    }
    planner = _planner([_response(hijack)] * 3)
    plan = await _run(planner)
    # The draft forbids the keys → invalid_response after retries.
    assert plan.metadata["fallback_reason"] == "invalid_response"
    assert plan.intervention_id != "int_evil"
    assert plan.consent_level == "suggest"


@pytest.mark.asyncio
async def test_degenerate_payload_is_rejected_not_placeholdered() -> None:
    empty = {**_VALID_DRAFT, "headline": "", "situation_summary": "", "micro_steps": []}
    planner = _planner([_response(empty)] * 3)
    plan = await _run(planner)
    assert plan.metadata["source"] == "fallback"
    assert plan.metadata["fallback_reason"] == "invalid_response"
    assert planner._sdk.messages.create.await_count == 3  # noqa: SLF001
    # Nothing was cached as a live plan: a fresh call goes to the SDK again.
    planner._sdk.messages.create.side_effect = [_response()]  # noqa: SLF001
    live = await _run(planner)
    assert live.metadata["source"] == "llm"


@pytest.mark.asyncio
async def test_usage_is_billed_even_when_the_payload_is_invalid(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path / "ledger.json", warn_usd=5.0, kill_usd=20.0)
    planner = _planner(
        [_response({"nope": 1}, input_tokens=1000, output_tokens=10)] * 3, tracker=tracker
    )
    await _run(planner)
    # 3 HTTP transactions × (1000 in + 10 out) on Sonnet 5 ($2 / $10 per MTok).
    assert tracker.today_total_usd() == pytest.approx(3 * (1000 * 2.0 + 10 * 10.0) / 1e6)
    assert tracker.prompt_tokens_today() == 3000
    assert tracker.completion_tokens_today() == 30


# ---------------------------------------------------------------------------
# Failure-mode discriminator + worst-case bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "mode"),
    [
        ("refusal", "empty_response"),
        ("bad_request", "empty_response"),
        ("model_unavailable", "empty_response"),
        ("api_error", "empty_response"),
        ("max_tokens_truncated", "parse_error"),
        ("context_window_exceeded", "parse_error"),
        ("invalid_response", "parse_error"),
        ("retries_exhausted", "timeout"),
    ],
)
def test_classify_plan_failure_mode_covers_new_reasons(reason: str, mode: str) -> None:
    plan = SimpleNamespace(metadata={"source": "fallback", "fallback_reason": reason})
    assert classify_plan_failure_mode(plan) == mode


def test_worst_case_seconds_is_attempts_times_timeout_plus_capped_backoff() -> None:
    cfg = LLMConfig()
    assert cfg.planner_attempts == 3
    assert cfg.planner_backoff_cap_seconds == 8.0
    assert cfg.planner_worst_case_seconds == pytest.approx(3 * 30.0 + (2.0 + 3.0))
    assert LLMConfig(timeout_seconds=10.0).planner_worst_case_seconds == pytest.approx(35.0)


def test_privacy_planner_delegates_worst_case_seconds() -> None:
    planner = _planner([_response()])
    wrapped = PrivacyAwarePlanner(planner._config, planner)  # noqa: SLF001
    assert wrapped.worst_case_seconds == planner.worst_case_seconds
    bare = PrivacyAwarePlanner(LLMConfig(timeout_seconds=10.0), None)
    assert bare.worst_case_seconds == pytest.approx(35.0)
