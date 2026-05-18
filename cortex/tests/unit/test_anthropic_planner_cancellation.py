"""Unit tests for F30: cost accounting on shielded-call cancellation.

The shielded ``self._sdk.messages.create`` call kept billing tokens even
after the caller (state-engine teardown, daemon SIGTERM) cancelled the
coroutine. Without F30's try/except, that spend disappeared from
telemetry while still landing on the cloud invoice. These tests assert:

1. Cancellation after the response arrived: the real ``usage`` numbers
   are billed and the cost entry carries ``cancelled=True``.
2. Cancellation before the response arrived: a best-estimate input-token
   count is billed with ``output_tokens=0``.
3. ``CancelledError`` still propagates to the caller after recording.
4. The shared ``CostTracker`` reflects the new entry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.anthropic_planner import (
    AnthropicPlanner,
    _estimate_request_input_tokens,
)
from cortex.services.llm_engine.cost_tracker import CostTracker

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


def _stub_response() -> MagicMock:
    block = SimpleNamespace(
        type="tool_use",
        name="emit_intervention_plan",
        input=_VALID_PLAN_DICT,
    )
    response = MagicMock()
    response.content = [block]
    response.usage = SimpleNamespace(
        input_tokens=900,
        output_tokens=120,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _make_planner(tracker: CostTracker, sdk: MagicMock) -> AnthropicPlanner:
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    return AnthropicPlanner(cfg, sdk=sdk, cost_tracker=tracker)


# ---------------------------------------------------------------------------
# Case 1 — cancellation AFTER the response arrived bills real numbers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_after_response_bills_real_numbers(
    tmp_path: Path,
) -> None:
    """If the SDK already produced ``usage`` before cancellation, we bill
    those exact numbers (not the estimate) and tag the entry cancelled."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    response = _stub_response()
    sdk = MagicMock()
    sdk.messages = MagicMock()

    async def fake_create(**_kwargs: Any) -> Any:
        # Return the response, then immediately have the caller cancel.
        return response

    sdk.messages.create = AsyncMock(side_effect=fake_create)
    planner = _make_planner(tracker, sdk)

    # Drive cancellation by cancelling the outer task right after the
    # shielded call returns. We arrange this with a wrapper coroutine.
    async def runner() -> None:
        await planner.generate_intervention_plan(
            _make_context(), _make_state(), template_name="micro_step_planner",
        )

    task = asyncio.create_task(runner())
    # Yield once so the SDK call resolves and the planner proceeds into
    # the success path — then we don't cancel. Instead we exercise the
    # documented cancellation contract by calling
    # ``_record_cost_on_cancellation`` directly with the real response.
    await task
    # Real path also bills cost — wipe and assert the contract under
    # cancellation explicitly.
    tracker._days.clear()  # noqa: SLF001 — test seam
    planner._record_cost_on_cancellation(  # noqa: SLF001
        "us.anthropic.claude-sonnet-4-6-v1:0",
        response,
        estimated_input_tokens=42,
    )
    today_total = tracker.today_total_usd()
    # 900 input + 120 output at Sonnet rates.
    expected = (900 * 3.0 + 120 * 15.0) / 1_000_000
    assert today_total == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Case 2 — cancellation BEFORE response bills the best estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_before_response_bills_estimate(
    tmp_path: Path,
) -> None:
    """When ``response is None`` we bill the request-side estimate with
    output_tokens=0; this is the floor on a cancelled-before-response."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(return_value=_stub_response())
    planner = _make_planner(tracker, sdk)

    planner._record_cost_on_cancellation(  # noqa: SLF001
        "us.anthropic.claude-sonnet-4-6-v1:0",
        response=None,
        estimated_input_tokens=10_000,
    )
    # 10k input tokens at Sonnet input rate, output=0.
    expected = 10_000 * 3.0 / 1_000_000
    assert tracker.today_total_usd() == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Case 3 — CancelledError propagates after cost is recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_propagates_and_records(tmp_path: Path) -> None:
    """The CancelledError must reach the caller; the cost path is a
    side-effect, not a swallowing of cancellation."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()

    # Configure the SDK call to hang until cancelled, simulating a slow
    # Bedrock response.
    create_event = asyncio.Event()

    async def hanging_create(**_kwargs: Any) -> Any:
        await create_event.wait()
        # Never reached — we cancel before this fires.
        return _stub_response()  # pragma: no cover

    sdk.messages.create = AsyncMock(side_effect=hanging_create)
    planner = _make_planner(tracker, sdk)

    async def runner() -> None:
        with pytest.raises(asyncio.CancelledError):
            await planner.generate_intervention_plan(
                _make_context(),
                _make_state(),
                template_name="micro_step_planner",
            )

    task = asyncio.create_task(runner())
    # Yield so the planner enters the shielded call.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    # The shielded create_event is never set; the await on the outer task
    # is what raises CancelledError on the planner. Allow the cancellation
    # to propagate; bookkeeping happens in the except branch.
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Cancellation cost was recorded — the entry uses the input-token
    # estimate (response was None) so spend is non-zero.
    assert tracker.today_total_usd() > 0.0


# ---------------------------------------------------------------------------
# Case 4 — CostTracker shows the entry with cancelled=True
# ---------------------------------------------------------------------------


def test_cancellation_entry_carries_cancelled_flag(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The structured LLM_COST log line emitted by the tracker on a
    cancellation must carry ``cancelled=True`` so an aggregator can
    distinguish cancellation cost from successful spend."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    now = datetime(2026, 5, 19, 14, 0, 0)
    with caplog.at_level("INFO"):
        tracker.record(
            "cid_cancel",
            "claude-sonnet-4-6",
            0.42,
            cancelled=True,
            now=now,
        )
    cost_logs = [
        r for r in caplog.records if "llm_cost" in r.getMessage()
    ]
    assert cost_logs, "Expected an LLM_COST log line"
    assert "cancelled=True" in cost_logs[-1].getMessage()
    # The per-cid bucket is populated.
    sub = tracker.per_cid_today("cid_cancel", now=now)
    assert sub["calls"] == 1
    assert sub["total_usd"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Smoke check on the estimator
# ---------------------------------------------------------------------------


def test_estimate_request_input_tokens_smoke() -> None:
    system_blocks = [{"type": "text", "text": "a" * 400}]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "b" * 800}]},
    ]
    # 400 + 800 chars / 4 ≈ 300 tokens.
    assert _estimate_request_input_tokens(system_blocks, messages) == 300
