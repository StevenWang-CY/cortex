"""audit-w2 — the daily-cost kill switch must re-fire between retries.

F20 added a ``CostTracker.check_budget()`` consulted at the top of
``AnthropicPlanner.generate_intervention_plan``. F30 added cost
accounting on cancellation. The retry loop, however, only consulted
the budget on attempt 1. A successful but token-heavy response on
attempt 1 (or a cancellation-billed entry from F30) can push the day's
spend over ``BUDGET_KILL`` mid-call; the loop would then continue to
attempts 2 and 3 and bill those too.

audit-w2 re-consults the budget at the top of every retry. If the
ceiling is crossed mid-call, the planner returns the deterministic
fallback plan stamped with ``budget_killed_on_retry`` so an operator
can tell apart "killed at call start" from "killed mid-retry".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import RateLimitError

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner
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
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9, overall=0.9,
        ),
        timestamp=100.0,
        dwell_seconds=35.0,
    )


def _planner(tracker: CostTracker, sdk: MagicMock) -> AnthropicPlanner:
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    return AnthropicPlanner(cfg, sdk=sdk, cost_tracker=tracker)


def _rate_limit_error() -> RateLimitError:
    # The Anthropic SDK error needs a response object; a SimpleNamespace
    # with the attributes the constructor pokes is enough for our retry
    # path — we never serialise it.
    return RateLimitError(
        "rate limited",
        response=SimpleNamespace(
            status_code=429,
            headers={},
            request=SimpleNamespace(method="POST", url="x"),
        ),
        body=None,
    )


@pytest.mark.asyncio
async def test_kill_switch_fires_mid_retry_after_budget_crossed(
    tmp_path: Path,
) -> None:
    """First attempt fails with a 429 (no cost billed). Before retry 2
    we sneak a $30 charge into the tracker — the loop must observe the
    new state and serve the deterministic fallback rather than burning
    attempt 2."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()

    call_count = {"n": 0}

    async def flaky_create(**_kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First attempt: rate-limit error. The planner enters the
            # retry path and sleeps before attempt 2.
            raise _rate_limit_error()
        # Should never reach here — the budget kill at retry 2 must
        # short-circuit before the SDK is called again.
        raise AssertionError(
            "planner consulted SDK after the kill switch should have fired"
        )

    sdk.messages.create = AsyncMock(side_effect=flaky_create)
    planner = _planner(tracker, sdk)

    # Crossing the budget happens between attempts. We patch out the
    # exponential backoff sleep to make the test fast and inject the
    # cost mid-way through.
    import cortex.services.llm_engine.anthropic_planner as _p

    async def fast_sleep(_seconds: float) -> None:
        tracker.record(
            "cid_test",
            "us.anthropic.claude-sonnet-4-6-v1:0",
            25.0,  # over the $20 kill ceiling
            cancelled=False,
        )

    original_sleep = _p.asyncio.sleep
    _p.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        plan = await planner.generate_intervention_plan(
            _make_context(),
            _make_state(),
            template_name="micro_step_planner",
        )
    finally:
        _p.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # Exactly one SDK attempt before the kill fires — no double-bill.
    assert call_count["n"] == 1
    # Plan is the deterministic fallback, stamped with the mid-retry tag.
    assert plan.metadata.get("budget_killed") is True
    assert plan.metadata.get("fallback_reason") == "budget_killed"
    assert plan.metadata.get("budget_killed_on_retry") == 2


@pytest.mark.asyncio
async def test_first_attempt_unchanged_when_budget_ok(
    tmp_path: Path,
) -> None:
    """Regression guard: when the budget is healthy, the mid-retry
    re-check must not alter the legacy retry behaviour."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    sdk = MagicMock()
    sdk.messages = MagicMock()

    call_count = {"n": 0}

    async def flaky_then_succeed(**_kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _rate_limit_error()
        block = SimpleNamespace(
            type="tool_use",
            name="emit_intervention_plan",
            input={
                "level": "overlay_only",
                "headline": "Fix the NameError on line 10",
                "situation_summary": "1 error in main.py",
                "primary_focus": "main.py:10",
                "micro_steps": ["Read the NameError", "Define x"],
                "hide_targets": ["editor_symbols_except_current_function"],
                "ui_plan": {
                    "dim_background": False,
                    "show_overlay": True,
                    "fold_unrelated_code": True,
                    "intervention_type": "overlay_only",
                },
                "tone": "supportive",
                "suggested_actions": [],
                "causal_explanation": "1 error pulled focus off the function.",
            },
        )
        return SimpleNamespace(
            content=[block],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    sdk.messages.create = AsyncMock(side_effect=flaky_then_succeed)
    planner = _planner(tracker, sdk)

    import cortex.services.llm_engine.anthropic_planner as _p

    async def fast_sleep(_seconds: float) -> None:
        return None

    original_sleep = _p.asyncio.sleep
    _p.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        plan = await planner.generate_intervention_plan(
            _make_context(),
            _make_state(),
            template_name="micro_step_planner",
        )
    finally:
        _p.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # Both attempts ran (1 fail + 1 success), kill switch never fired.
    assert call_count["n"] == 2
    assert plan.metadata.get("budget_killed") is None
    assert plan.metadata.get("budget_killed_on_retry") is None


@pytest.mark.asyncio
async def test_planner_without_cost_tracker_skips_recheck(
    tmp_path: Path,
) -> None:
    """If ``cost_tracker`` is ``None`` (BYOK keychain failure path),
    the re-check is a no-op and the legacy retry behaviour is preserved.
    Asserting this explicitly closes the "telemetry disabled" branch."""
    sdk = MagicMock()
    sdk.messages = MagicMock()

    call_count = {"n": 0}

    async def flaky_then_succeed(**_kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _rate_limit_error()
        block = SimpleNamespace(
            type="tool_use",
            name="emit_intervention_plan",
            input={
                "level": "overlay_only",
                "headline": "Fix the NameError on line 10",
                "situation_summary": "1 error in main.py",
                "primary_focus": "main.py:10",
                "micro_steps": ["Read the NameError", "Define x"],
                "hide_targets": ["editor_symbols_except_current_function"],
                "ui_plan": {
                    "dim_background": False,
                    "show_overlay": True,
                    "fold_unrelated_code": True,
                    "intervention_type": "overlay_only",
                },
                "tone": "supportive",
                "suggested_actions": [],
                "causal_explanation": "1 error pulled focus off the function.",
            },
        )
        return SimpleNamespace(
            content=[block],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    sdk.messages.create = AsyncMock(side_effect=flaky_then_succeed)
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    planner = AnthropicPlanner(cfg, sdk=sdk, cost_tracker=None)

    import cortex.services.llm_engine.anthropic_planner as _p

    async def fast_sleep(_seconds: float) -> None:
        return None

    original_sleep = _p.asyncio.sleep
    _p.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        plan = await planner.generate_intervention_plan(
            _make_context(),
            _make_state(),
            template_name="micro_step_planner",
        )
    finally:
        _p.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # Retry succeeded — no kill metadata.
    assert call_count["n"] == 2
    assert plan.metadata.get("budget_killed") is None
