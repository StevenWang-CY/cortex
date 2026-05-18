"""Unit tests for F27: circuit-breaker fallback surfaced + excluded from learning.

The circuit breaker opens after consecutive Bedrock failures and serves
``build_fallback_plan`` silently. Pre-F27, the user could not tell their
generic "step away from the screen" plan came from the rule-based path,
and their dismissals trained the dismissal model against the LLM-side
ground truth — which made real Bedrock recommendations harder to fire
once the breaker closed.

These tests verify:

1. Every fallback plan is stamped with ``metadata["source"] = "fallback"``.
2. ``TriggerPolicy.record_outcome(is_fallback_origin=True)`` skips the
   dismissal-model update.
3. A real-plan dismissal still trains the dismissal model.
4. Breaker recovery flips off — the next plan is not stamped.
5. The overlay shows the fallback hint when metadata says so, and hides
   it otherwise.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.libs.config.settings import BedrockConfig, InterventionConfig, LLMConfig
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner
from cortex.services.llm_engine.client import build_fallback_plan
from cortex.services.state_engine.trigger_policy import (
    Outcome,
    TriggerPolicy,
)

# ---------------------------------------------------------------------------
# Helpers
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
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _make_planner(sdk: MagicMock | None = None) -> AnthropicPlanner:
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
    )
    if sdk is None:
        sdk = MagicMock()
        sdk.messages = MagicMock()
        sdk.messages.create = AsyncMock(return_value=_stub_response())
    return AnthropicPlanner(cfg, sdk=sdk)


# ---------------------------------------------------------------------------
# 1. Every fallback plan is stamped with source=fallback
# ---------------------------------------------------------------------------


def test_build_fallback_plan_stamps_source_metadata() -> None:
    plan = build_fallback_plan(_make_context())
    assert plan.metadata.get("source") == "fallback"


@pytest.mark.asyncio
async def test_planner_breaker_open_stamps_fallback_reason() -> None:
    import time as _time

    planner = _make_planner()
    planner._circuit._opened_at = _time.monotonic()  # noqa: SLF001
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.metadata.get("source") == "fallback"
    assert plan.metadata.get("fallback_reason") == "circuit_open"


# ---------------------------------------------------------------------------
# 2. Fallback-origin dismissal does NOT train the dismissal model
# ---------------------------------------------------------------------------


def test_fallback_dismissal_skipped_from_training(tmp_path) -> None:
    cfg = InterventionConfig()
    policy = TriggerPolicy(
        cfg,
        dismissal_model_path=tmp_path / "model.json",
        quiet_mode_history_path=tmp_path / "quiet.json",
    )
    weights_before = policy._dismissal_model_weights  # noqa: SLF001
    outcomes_before = policy._dismissal_outcomes  # noqa: SLF001

    outcome = Outcome(
        dismissed=True,
        confidence=0.9,
        context_complexity=0.6,
        typing_burst_seconds=2.0,
        is_fallback_origin=True,
    )
    policy.record_outcome(
        dismissed=outcome.dismissed,
        confidence=outcome.confidence,
        context_complexity=outcome.context_complexity,
        typing_burst_seconds=outcome.typing_burst_seconds,
        is_fallback_origin=outcome.is_fallback_origin,
    )

    # Aggregate counter still ticks (real user behaviour).
    assert policy._dismissals_total == 1  # noqa: SLF001
    # Dismissal-model outcomes counter did NOT advance.
    assert policy._dismissal_outcomes == outcomes_before  # noqa: SLF001
    # Weights were not perturbed.
    assert policy._dismissal_model_weights == weights_before  # noqa: SLF001


# ---------------------------------------------------------------------------
# 3. Real-plan dismissal DOES train the model
# ---------------------------------------------------------------------------


def test_real_dismissal_counts_into_training(tmp_path) -> None:
    cfg = InterventionConfig()
    policy = TriggerPolicy(
        cfg,
        dismissal_model_path=tmp_path / "model.json",
        quiet_mode_history_path=tmp_path / "quiet.json",
    )

    policy.record_outcome(
        dismissed=True,
        confidence=0.9,
        context_complexity=0.6,
        typing_burst_seconds=2.0,
        is_fallback_origin=False,
    )
    assert policy._dismissal_outcomes == 1  # noqa: SLF001
    # Weights moved off the (0, 0, 0) starting point.
    weights = policy._dismissal_model_weights  # noqa: SLF001
    assert any(abs(w) > 1e-6 for w in weights), (
        "expected logistic SGD to perturb the weights on a real dismissal"
    )


# ---------------------------------------------------------------------------
# 4. Breaker recovery: next plan is not stamped fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_recovery_drops_fallback_metadata() -> None:
    planner = _make_planner()
    # The breaker is closed (default state) — a successful round-trip
    # produces a plan with no ``source`` metadata at all.
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.metadata.get("source") != "fallback"


# ---------------------------------------------------------------------------
# 5. Overlay shows the hint only when source=fallback
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _qt_offscreen() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_overlay_hint_visible_only_on_fallback() -> None:
    """The overlay's ``show_intervention`` must surface the offline hint
    when ``metadata["source"] == "fallback"`` and hide it otherwise.

    We patch out the showEvent hooks (``apply_unified_titlebar`` /
    ``apply_vibrancy``) and ``self.show()`` itself so the test exercises
    only the payload-handling logic. Without that, the Qt show event
    enters the mac_native NSWindow plumbing which is unsafe under the
    offscreen platform.
    """
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    from cortex.apps.desktop_shell import overlay as overlay_mod
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    app = QApplication.instance() or QApplication([])
    _ = app

    # Patch the show-path side effects so the offscreen Qt platform
    # never reaches the macOS NSWindow code (which segfaults under
    # headless test runs).
    original_show = OverlayWindow.show
    original_raise = OverlayWindow.raise_
    original_activate = OverlayWindow.activateWindow
    OverlayWindow.show = lambda self: None  # type: ignore[method-assign]
    OverlayWindow.raise_ = lambda self: None  # type: ignore[method-assign]
    OverlayWindow.activateWindow = lambda self: None  # type: ignore[method-assign]
    try:
        overlay = OverlayWindow()
        real_payload = {
            "intervention_id": "int_real",
            "headline": "Step away",
            "situation_summary": "Real summary",
            "primary_focus": "Real focus",
            "micro_steps": ["Read the error"],
            "ui_plan": {"show_overlay": True},
            "level": "overlay_only",
        }
        overlay.show_intervention(real_payload)
        assert overlay._fallback_hint.text() == ""  # noqa: SLF001

        fallback_payload = dict(real_payload)
        fallback_payload["intervention_id"] = "int_fallback"
        fallback_payload["metadata"] = {
            "source": "fallback",
            "fallback_reason": "circuit_open",
        }
        overlay.show_intervention(fallback_payload)
        assert overlay._fallback_hint.text() != ""  # noqa: SLF001
        assert "offline" in overlay._fallback_hint.text().lower()  # noqa: SLF001

        overlay.deleteLater()
    finally:
        OverlayWindow.show = original_show  # type: ignore[method-assign]
        OverlayWindow.raise_ = original_raise  # type: ignore[method-assign]
        OverlayWindow.activateWindow = original_activate  # type: ignore[method-assign]
    _ = overlay_mod  # silence "unused import" linters
