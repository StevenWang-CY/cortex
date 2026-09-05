"""Cancellation accounting for shielded SDK calls (audit F30 / D4).

The SDK call runs as its own task behind ``asyncio.shield``. When the
caller (state-engine teardown, daemon SIGTERM, the daemon's outer
``wait_for``) is cancelled mid-flight the task keeps running; the old
code recorded a request-side *estimate* at cancel time, lost the real
usage, never surfaced the orphan's failure, and leaked its semaphore
slot. These tests assert the done-callback design:

1. Cancel, then let the orphaned task complete → its real ``usage`` is
   billed (tagged ``cancelled=True``) and the semaphore slot is released.
2. Cancel, then the orphan fails → nothing billed, slot released.
3. Cancel, then the orphan itself is cancelled (event-loop teardown) →
   the request-side estimate is billed with ``output_tokens=0``.
4. ``CancelledError`` still propagates to the caller.
5. The ``_record_cost_on_cancellation`` helper bills real numbers when a
   response exists and the estimate otherwise.
"""

from __future__ import annotations

import asyncio
import json
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


def _stub_response() -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(_VALID_DRAFT))],
        usage=SimpleNamespace(
            input_tokens=900,
            output_tokens=120,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _make_planner(tracker: CostTracker | None, sdk: MagicMock) -> AnthropicPlanner:
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    return AnthropicPlanner(
        cfg,
        sdk=sdk,
        cost_tracker=tracker,
        _allow_unbrokered_test_requests=True,
    )


def _semaphore_slots(planner: AnthropicPlanner) -> int:
    return planner._semaphore._value  # type: ignore[attr-defined]  # noqa: SLF001


async def _settle() -> None:
    """Let the orphaned task and its done-callback run."""
    for _ in range(6):
        await asyncio.sleep(0)


async def _cancel_in_flight(
    planner: AnthropicPlanner,
    gate: asyncio.Event,
) -> asyncio.Task[None]:
    """Start a planner call, wait until the SDK call is in flight, cancel it."""

    async def runner() -> None:
        await planner.generate_intervention_plan(
            _make_context(), _make_state(), template_name="micro_step_planner",
        )

    task = asyncio.create_task(runner())
    await gate.wait()  # the fake SDK call has started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return task


# Sonnet 5 on Bedrock Mantle: $2 / $10 per MTok.
_SONNET5_IN = 2.0
_SONNET5_OUT = 10.0


# ---------------------------------------------------------------------------
# Case 1 — orphan completes: real usage billed, slot released
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_call_completion_bills_real_usage_and_releases_slot(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_create(**_kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return _stub_response()

    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=slow_create)
    planner = _make_planner(tracker, sdk)

    with caplog.at_level("INFO"):
        await _cancel_in_flight(planner, started)
        # The caller is gone but the HTTP transaction is still running:
        # nothing billed yet, slot still held by the orphan.
        assert tracker.today_total_usd() == pytest.approx(0.0)
        assert _semaphore_slots(planner) == 1

        release.set()
        await _settle()

    expected = (900 * _SONNET5_IN + 120 * _SONNET5_OUT) / 1_000_000
    assert tracker.today_total_usd() == pytest.approx(expected)
    assert tracker.prompt_tokens_today() == 900
    assert tracker.completion_tokens_today() == 120
    assert _semaphore_slots(planner) == 2
    cost_logs = [r.getMessage() for r in caplog.records if "llm_cost" in r.getMessage()]
    assert cost_logs and "cancelled=True" in cost_logs[-1]
    assert any("orphan_completed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Case 2 — orphan fails: nothing billed, slot released, failure logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_call_failure_releases_slot_without_billing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def failing_create(**_kwargs: Any) -> Any:
        started.set()
        await release.wait()
        raise RuntimeError("connection reset by peer")

    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=failing_create)
    planner = _make_planner(tracker, sdk)

    with caplog.at_level("WARNING"):
        await _cancel_in_flight(planner, started)
        release.set()
        await _settle()

    assert tracker.today_total_usd() == pytest.approx(0.0)
    assert _semaphore_slots(planner) == 2
    assert any("orphan_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Case 3 — orphan itself cancelled (loop teardown): estimate billed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_cancelled_at_teardown_bills_the_estimate(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    started = asyncio.Event()
    inner: dict[str, asyncio.Task[Any] | None] = {"task": None}

    async def hanging_create(**_kwargs: Any) -> Any:
        inner["task"] = asyncio.current_task()
        started.set()
        await asyncio.Event().wait()  # never completes on its own
        return _stub_response()  # pragma: no cover

    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=hanging_create)
    planner = _make_planner(tracker, sdk)

    await _cancel_in_flight(planner, started)
    assert tracker.today_total_usd() == pytest.approx(0.0)
    orphan = inner["task"]
    assert orphan is not None and not orphan.done()

    # Simulate event-loop teardown cancelling the orphaned task.
    orphan.cancel()
    await _settle()

    # Request-side estimate with output_tokens=0 — non-zero because the
    # assembled prompt is thousands of characters.
    assert tracker.today_total_usd() > 0.0
    assert tracker.prompt_tokens_today() > 0
    assert tracker.completion_tokens_today() == 0
    assert _semaphore_slots(planner) == 2


# ---------------------------------------------------------------------------
# Case 4 — CancelledError propagates; no accounting happens at cancel time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_propagates_to_the_caller(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_create(**_kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return _stub_response()

    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=slow_create)
    planner = _make_planner(tracker, sdk)

    task = await _cancel_in_flight(planner, started)
    assert task.cancelled()
    release.set()
    await _settle()


# ---------------------------------------------------------------------------
# Case 5 — the accounting helper
# ---------------------------------------------------------------------------


def test_record_cost_on_cancellation_bills_real_numbers_when_response_exists(
    tmp_path: Path,
) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()
    planner = _make_planner(tracker, sdk)
    planner._record_cost_on_cancellation(  # noqa: SLF001
        "us.anthropic.claude-sonnet-4-6-v1:0",  # legacy id still normalises
        _stub_response(),
        estimated_input_tokens=42,
    )
    # 900 input + 120 output at Sonnet 4.6 rates ($3 / $15 per MTok).
    expected = (900 * 3.0 + 120 * 15.0) / 1_000_000
    assert tracker.today_total_usd() == pytest.approx(expected)
    assert tracker.prompt_tokens_today() == 900


def test_record_cost_on_cancellation_bills_estimate_without_response(
    tmp_path: Path,
) -> None:
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()
    planner = _make_planner(tracker, sdk)
    planner._record_cost_on_cancellation(  # noqa: SLF001
        "anthropic.claude-sonnet-5",
        response=None,
        estimated_input_tokens=10_000,
    )
    expected = 10_000 * _SONNET5_IN / 1_000_000
    assert tracker.today_total_usd() == pytest.approx(expected)
    assert tracker.prompt_tokens_today() == 10_000
    assert tracker.completion_tokens_today() == 0


def test_cancellation_entry_carries_cancelled_flag(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The structured LLM_COST log line emitted by the tracker on a
    cancellation must carry ``cancelled=True`` so an aggregator can
    distinguish cancellation cost from successful spend."""
    tracker = CostTracker(tmp_path / "cost_ledger.json", warn_usd=5.0, kill_usd=20.0)
    now = datetime(2026, 5, 19, 14, 0, 0)
    with caplog.at_level("INFO"):
        tracker.record(
            "cid_cancel",
            "claude-sonnet-5",
            0.42,
            cancelled=True,
            now=now,
        )
    cost_logs = [r for r in caplog.records if "llm_cost" in r.getMessage()]
    assert cost_logs, "Expected an LLM_COST log line"
    assert "cancelled=True" in cost_logs[-1].getMessage()
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
