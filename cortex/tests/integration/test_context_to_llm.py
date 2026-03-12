"""
Integration Test: Context → LLM → InterventionPlan Pipeline

Tests the flow from workspace context assembly through prompt building,
mock LLM response, JSON parsing, and InterventionPlan validation.

Also includes privacy verification and performance benchmarks.

Verifies:
- Context assembly produces valid TaskContext
- Prompt templates are selected correctly by mode
- build_messages produces well-formed messages
- Parser handles valid and malformed LLM output
- InterventionPlan validation catches destructive actions
- No biometric data leaks into LLM prompts (privacy)
- Pipeline performance within budgets
"""

from __future__ import annotations

import json
import time

import pytest

from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.features import FeatureVector, KinematicFeatures, PhysioFeatures, TelemetryFeatures
from cortex.libs.schemas.intervention import InterventionPlan, UIPlan
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.context_engine.app_classifier import classify_app, classify_mode
from cortex.services.llm_engine.parser import parse_and_validate, parse_llm_response
from cortex.services.llm_engine.prompts import (
    build_messages,
    build_user_prompt,
    select_prompt_template,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import ScoreSmoother


# ============================================================================
# Helpers
# ============================================================================


def _flow_physio() -> PhysioFeatures:
    return PhysioFeatures(
        pulse_bpm=72.0, pulse_quality=0.9,
        pulse_variability_proxy=55.0, hr_delta_5s=0.5, valid=True,
    )


def _flow_kinematics() -> KinematicFeatures:
    return KinematicFeatures(
        blink_rate=16.0, blink_rate_delta=0.0, blink_suppression_score=0.0,
        head_pitch=0.0, head_yaw=0.0, head_roll=0.0,
        slump_score=0.1, forward_lean_score=0.1, shoulder_drop_ratio=0.05,
        confidence=0.9,
    )


def _flow_telemetry() -> TelemetryFeatures:
    return TelemetryFeatures(
        mouse_velocity_mean=400.0, mouse_velocity_variance=5000.0,
        mouse_jerk_score=0.1, click_burst_score=0.1, click_frequency=0.5,
        keyboard_burst_score=0.2, keystroke_interval_variance=500.0,
        backspace_density=0.05, inactivity_seconds=2.0, window_switch_rate=5.0,
    )


def _make_state_estimate(
    state: str = "HYPER", confidence: float = 0.90, dwell: float = 10.0
) -> StateEstimate:
    """Create a StateEstimate for testing."""
    return StateEstimate(
        state=state,
        confidence=confidence,
        scores=StateScores(flow=0.2, hypo=0.1, hyper=0.9, recovery=0.1),
        reasons=["Elevated overwhelm indicators"],
        signal_quality=SignalQuality(physio=0.8, kinematics=0.85, telemetry=0.9),
        timestamp=1000.0,
        dwell_seconds=dwell,
    )


def _make_editor_context(*, errors: int = 3) -> EditorContext:
    diags = [
        Diagnostic(severity="error", message=f"Type error {i}", line=10 + i)
        for i in range(errors)
    ]
    return EditorContext(
        file_path="src/app.ts",
        visible_range=(1, 50),
        symbol_at_cursor="handleSubmit",
        diagnostics=diags,
        visible_code="function handleSubmit() { /* ... */ }",
    )


def _make_terminal_context(*, errors: bool = True) -> TerminalContext:
    return TerminalContext(
        last_n_lines=["$ npm test", "FAIL src/app.test.ts", "TypeError: x is not a function"],
        detected_errors=["TypeError: x is not a function"] if errors else [],
        repeated_commands=["npm test"],
    )


def _make_browser_context(*, tabs: int = 8) -> BrowserContext:
    all_tabs = [
        TabInfo(title=f"Tab {i}", url=f"https://example.com/{i}", tab_type="other")
        for i in range(tabs)
    ]
    all_tabs[0] = TabInfo(
        title="React Docs", url="https://react.dev/docs", tab_type="documentation", is_active=True
    )
    return BrowserContext(
        active_tab_title="React Docs",
        active_tab_url="https://react.dev/docs",
        active_tab_content_excerpt="React is a JavaScript library for building UIs.",
        all_tabs=all_tabs,
        tab_type_classification={"documentation": 1, "other": tabs - 1},
    )


def _make_coding_context() -> TaskContext:
    """Context: coding with errors."""
    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.75,
        editor_context=_make_editor_context(),
        terminal_context=_make_terminal_context(),
    )


def _make_browsing_context() -> TaskContext:
    """Context: browsing with many tabs."""
    return TaskContext(
        mode="browsing",
        active_app="chrome",
        complexity_score=0.60,
        browser_context=_make_browser_context(tabs=12),
    )


def _valid_llm_json() -> str:
    """Valid LLM response JSON."""
    return json.dumps({
        "situation_summary": "User debugging a TypeError in handleSubmit",
        "primary_focus": "Fix the TypeError in src/app.ts line 12",
        "headline": "Fix TypeError in handleSubmit",
        "micro_steps": [
            "Check the function signature of handleSubmit",
            "Verify the argument types match the expected interface",
        ],
        "hide_targets": [
            "editor_symbols_except_current_function",
            "terminal_lines_before_last_error_block",
        ],
        "ui_plan": {
            "dim_background": True,
            "show_overlay": True,
            "fold_unrelated_code": True,
            "intervention_type": "simplified_workspace",
        },
        "tone": "direct",
    })


def _malformed_llm_json() -> str:
    """Malformed JSON with trailing comma and markdown fence."""
    return """```json
{
    "situation_summary": "User debugging errors",
    "primary_focus": "Fix the error",
    "headline": "Focus on the error",
    "micro_steps": ["Read the stack trace", "Find the source",],
    "hide_targets": [],
    "ui_plan": {
        "dim_background": true,
        "show_overlay": true,
        "fold_unrelated_code": false,
        "intervention_type": "overlay_only"
    },
    "tone": "direct"
}
```"""


# ============================================================================
# Context Assembly Tests
# ============================================================================


class TestContextAssembly:
    """Test workspace context assembly."""

    def test_coding_mode_classification(self):
        app = classify_app("Visual Studio Code")
        assert app == "vscode"
        mode = classify_mode(app, editor_context=_make_editor_context())
        assert mode == "coding_debugging"

    def test_terminal_error_mode(self):
        app = classify_app("Terminal")
        assert app == "terminal"
        mode = classify_mode(app, terminal_context=_make_terminal_context())
        assert mode == "terminal_errors"

    def test_browsing_mode(self):
        app = classify_app("Google Chrome")
        assert app == "chrome"
        mode = classify_mode(app, browser_context=_make_browser_context())
        assert mode == "browsing"

    def test_task_context_llm_string(self):
        ctx = _make_coding_context()
        llm_text = ctx.to_llm_context()
        assert "coding_debugging" in llm_text
        assert "src/app.ts" in llm_text
        assert "TypeError" in llm_text

    def test_total_errors_across_contexts(self):
        ctx = _make_coding_context()
        assert ctx.total_errors == 4  # 3 editor + 1 terminal


# ============================================================================
# Prompt Selection and Building
# ============================================================================


class TestPromptPipeline:
    """Test prompt template selection and message building."""

    def test_coding_debug_selects_debug_template(self):
        ctx = _make_coding_context()
        template = select_prompt_template(ctx)
        assert template == "debug_error_summary"

    def test_browsing_selects_browser_template(self):
        ctx = _make_browsing_context()
        template = select_prompt_template(ctx)
        assert template == "browser_tab_reduction"

    def test_build_messages_structure(self):
        ctx = _make_coding_context()
        state = _make_state_estimate()
        messages = build_messages(ctx, state)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "JSON" in messages[0]["content"]
        assert "HYPER" in messages[1]["content"]

    def test_build_user_prompt_contains_context(self):
        ctx = _make_coding_context()
        state = _make_state_estimate()
        prompt = build_user_prompt(ctx, state)

        assert "src/app.ts" in prompt
        assert "TypeError" in prompt
        assert "HYPER" in prompt


# ============================================================================
# LLM Response Parsing
# ============================================================================


class TestLLMParsing:
    """Test parsing pipeline from raw LLM output to InterventionPlan."""

    def test_valid_json_to_plan(self):
        raw = _valid_llm_json()
        plan = parse_and_validate(raw)
        assert plan is not None
        assert plan.headline == "Fix TypeError in handleSubmit"
        assert len(plan.micro_steps) == 2
        assert plan.ui_plan.intervention_type == "simplified_workspace"

    def test_malformed_json_still_parses(self):
        raw = _malformed_llm_json()
        plan = parse_and_validate(raw)
        assert plan is not None
        assert plan.headline == "Focus on the error"

    def test_plan_validation(self):
        plan = parse_and_validate(_valid_llm_json())
        assert plan is not None
        assert plan.is_valid
        assert not plan.is_destructive

    def test_empty_string_returns_none(self):
        plan = parse_and_validate("")
        assert plan is None

    def test_garbage_returns_none(self):
        plan = parse_and_validate("not json at all!!!")
        assert plan is None


# ============================================================================
# Full Context → Prompt → Parse → Plan Pipeline
# ============================================================================


class TestContextToLLMPipeline:
    """Test the complete pipeline: context → prompt → mock LLM → parse → plan."""

    def test_coding_debug_full_pipeline(self):
        """coding_debugging context → debug prompt → valid plan."""
        ctx = _make_coding_context()
        state = _make_state_estimate()

        # Build prompt
        messages = build_messages(ctx, state)
        assert len(messages) == 2

        # Simulate LLM response
        raw = _valid_llm_json()
        plan = parse_and_validate(raw)

        assert plan is not None
        assert plan.is_valid
        assert not plan.is_destructive

    def test_browsing_full_pipeline(self):
        """browsing context → browser reduction prompt → valid plan."""
        ctx = _make_browsing_context()
        state = _make_state_estimate()

        messages = build_messages(ctx, state)
        assert "Tabs:" in messages[1]["content"] or "tabs" in messages[1]["content"].lower()

        # Simulate a browser-focused LLM response
        raw = json.dumps({
            "situation_summary": "Too many browser tabs open",
            "primary_focus": "Close irrelevant tabs",
            "headline": "Reduce tab clutter",
            "micro_steps": [
                "Keep React Docs tab open",
                "Close social and unrelated tabs",
            ],
            "hide_targets": ["browser_tabs_except_active"],
            "ui_plan": {
                "dim_background": True,
                "show_overlay": True,
                "fold_unrelated_code": False,
                "intervention_type": "overlay_only",
            },
            "tone": "direct",
        })

        plan = parse_and_validate(raw)
        assert plan is not None
        assert "browser_tabs_except_active" in plan.hide_targets


# ============================================================================
# End-to-End: HYPER → Context → LLM → Intervention → Recovery → Restore
# ============================================================================


class TestEndToEnd:
    """Full end-to-end integration test with mocked services."""

    def test_hyper_to_intervention_to_recovery(self):
        """
        E2E: detect HYPER → build context → generate plan → trigger →
        execute → detect recovery → restore.
        """
        from cortex.services.intervention_engine.executor import InterventionExecutor
        from cortex.services.intervention_engine.planner import prepare_plan
        from cortex.services.intervention_engine.restore import RestoreManager
        from cortex.services.intervention_engine.snapshot import capture_snapshot
        from cortex.services.intervention_engine.trigger import InterventionTrigger

        # --- Step 1: State Engine detects HYPER ---
        hyper_estimate = _make_state_estimate(
            state="HYPER", confidence=0.92, dwell=10.0
        )
        assert hyper_estimate.is_overwhelmed
        assert hyper_estimate.should_intervene

        # --- Step 2: Trigger evaluates ---
        trigger = InterventionTrigger()
        decision = trigger.evaluate(hyper_estimate, complexity_score=0.75)
        assert decision.should_trigger
        assert decision.level is not None

        # --- Step 3: Build context and get LLM plan ---
        ctx = _make_coding_context()
        messages = build_messages(ctx, hyper_estimate)
        assert len(messages) == 2

        # Simulate LLM response
        plan = parse_and_validate(_valid_llm_json())
        assert plan is not None

        # --- Step 4: Validate and prepare plan ---
        validation, commands = prepare_plan(plan)
        assert validation.is_valid

        # --- Step 5: Capture snapshot before intervention ---
        snapshot = capture_snapshot(ctx, plan.intervention_id)
        assert snapshot.intervention_id == plan.intervention_id

        # --- Step 6: Execute intervention (mock adapter) ---
        class MockAdapter:
            def __init__(self):
                self.calls = []

            async def execute(self, action, params):
                self.calls.append((action, params))
                return True

        executor = InterventionExecutor()
        mock_editor = MockAdapter()
        mock_browser = MockAdapter()
        executor.register_adapter("editor", mock_editor)
        executor.register_adapter("browser", mock_browser)

        # --- Step 7: Start restore manager ---
        restore = RestoreManager(executor=executor)
        intervention = restore.start_intervention(
            plan.intervention_id, snapshot, started_at=1000.0
        )
        assert intervention is not None

        # --- Step 8: Simulate recovery (FLOW state) ---
        flow_estimate = _make_state_estimate(
            state="FLOW", confidence=0.80, dwell=20.0
        )
        flow_estimate = StateEstimate(
            state="FLOW",
            confidence=0.80,
            scores=StateScores(flow=0.85, hypo=0.05, hyper=0.1, recovery=0.2),
            reasons=["Stable, focused engagement pattern"],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.85, telemetry=0.9),
            timestamp=1020.0,
            dwell_seconds=20.0,
        )

        # The restore manager tracks recovery, but with sustained FLOW >0.70
        # for 15s, it would auto-restore. We test the dismiss path here.

        # --- Step 9: User dismisses ---
        import asyncio

        outcome = asyncio.new_event_loop().run_until_complete(
            restore.dismiss(plan.intervention_id, current_time=1025.0)
        )
        assert outcome is not None
        assert outcome.user_action == "dismissed"

    def test_e2e_timing_under_budget(self):
        """The entire mock pipeline should complete in < 1 second."""
        start = time.monotonic()

        # Feature fusion + scoring
        from cortex.services.state_engine.feature_fusion import FeatureFusion
        from cortex.services.state_engine.rule_scorer import RuleScorer

        fusion = FeatureFusion()
        scorer = RuleScorer()

        ts = 1000.0
        fusion.update_physio(_flow_physio(), ts)
        fusion.update_kinematics(_flow_kinematics(), ts)
        fusion.update_telemetry(_flow_telemetry(), ts)
        vector, quality = fusion.fuse(ts)
        scores = scorer.compute_scores(vector)

        # Context and prompts
        ctx = _make_coding_context()
        state = _make_state_estimate()
        messages = build_messages(ctx, state)

        # Parse plan
        plan = parse_and_validate(_valid_llm_json())

        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Pipeline took {elapsed:.3f}s (budget: < 1.0s)"


# ============================================================================
# Privacy Verification
# ============================================================================


class TestPrivacy:
    """Verify no biometric data leaks into LLM requests."""

    def test_no_heart_rate_in_prompt(self):
        """LLM prompt should not contain raw HR values."""
        ctx = _make_coding_context()
        state = _make_state_estimate()
        messages = build_messages(ctx, state)

        full_text = " ".join(m["content"] for m in messages)
        # The state estimate has HR=72 but that shouldn't be in the prompt
        assert "pulse_bpm" not in full_text.lower()
        assert "heart rate" not in full_text.lower()
        assert "rmssd" not in full_text.lower()

    def test_no_biometric_features_in_context(self):
        """TaskContext.to_llm_context() should not include biometric data."""
        ctx = _make_coding_context()
        llm_text = ctx.to_llm_context()

        biometric_terms = [
            "pulse_bpm", "heart_rate", "hrv", "rmssd",
            "blink_rate", "ear_threshold", "shoulder_drop",
            "mouse_velocity_mean", "keystroke_interval",
        ]
        for term in biometric_terms:
            assert term not in llm_text.lower(), (
                f"Biometric term '{term}' found in LLM context"
            )

    def test_no_raw_frame_data_in_context(self):
        """No raw image/frame data should appear in any LLM input."""
        ctx = _make_coding_context()
        state = _make_state_estimate()
        messages = build_messages(ctx, state)

        full_text = " ".join(m["content"] for m in messages)
        frame_terms = ["frame", "pixel", "rgb", "bgr", "opencv", "webcam"]
        for term in frame_terms:
            assert term not in full_text.lower(), (
                f"Frame data term '{term}' found in LLM prompt"
            )

    def test_intervention_plan_no_biometrics(self):
        """InterventionPlan should not contain biometric info."""
        plan = parse_and_validate(_valid_llm_json())
        assert plan is not None

        plan_text = plan.model_dump_json()
        biometric_terms = ["heart_rate", "pulse_bpm", "hrv", "blink_rate"]
        for term in biometric_terms:
            assert term not in plan_text.lower()


# ============================================================================
# Performance Benchmarks
# ============================================================================


class TestPerformance:
    """Performance benchmarks for pipeline stages."""

    def test_feature_fusion_under_10ms(self):
        """Feature fusion should complete in < 10ms."""
        fusion = FeatureFusion()
        ts = 1000.0
        fusion.update_physio(_flow_physio(), ts)
        fusion.update_kinematics(_flow_kinematics(), ts)
        fusion.update_telemetry(_flow_telemetry(), ts)

        start = time.monotonic()
        for i in range(100):
            fusion.fuse(ts + i)
        elapsed = (time.monotonic() - start) / 100

        assert elapsed < 0.010, f"Fusion: {elapsed*1000:.2f}ms (budget: 10ms)"

    def test_classification_under_5ms(self):
        """State classification (scoring) should complete in < 5ms."""
        scorer = RuleScorer()
        vector = FeatureVector(
            timestamp=1000.0,
            hr=72.0, hrv_rmssd=55.0, hr_delta=0.5,
            blink_rate=16.0, blink_rate_delta=0.0,
            shoulder_drop_ratio=0.05, forward_lean_angle=5.0,
            mouse_velocity_mean=400.0, mouse_velocity_variance=5000.0,
            click_frequency=0.5, keystroke_interval_variance=500.0,
            tab_switch_frequency=5.0,
        )

        start = time.monotonic()
        for _ in range(100):
            scorer.compute_scores(vector)
        elapsed = (time.monotonic() - start) / 100

        assert elapsed < 0.005, f"Scoring: {elapsed*1000:.2f}ms (budget: 5ms)"

    def test_prompt_building_under_5ms(self):
        """Prompt building should complete in < 5ms."""
        ctx = _make_coding_context()
        state = _make_state_estimate()

        start = time.monotonic()
        for _ in range(100):
            build_messages(ctx, state)
        elapsed = (time.monotonic() - start) / 100

        assert elapsed < 0.005, f"Prompt: {elapsed*1000:.2f}ms (budget: 5ms)"

    def test_json_parsing_under_5ms(self):
        """JSON parsing + validation should complete in < 5ms."""
        raw = _valid_llm_json()

        start = time.monotonic()
        for _ in range(100):
            parse_and_validate(raw)
        elapsed = (time.monotonic() - start) / 100

        assert elapsed < 0.005, f"Parsing: {elapsed*1000:.2f}ms (budget: 5ms)"

    def test_full_pipeline_under_200ms(self):
        """
        Full signal-to-state pipeline should complete in < 200ms.
        (Without actual LLM call)
        """
        from cortex.services.state_engine.feature_fusion import FeatureFusion
        from cortex.services.state_engine.rule_scorer import RuleScorer
        from cortex.services.state_engine.smoother import ScoreSmoother

        start = time.monotonic()

        fusion = FeatureFusion()
        scorer = RuleScorer()
        smoother = ScoreSmoother()

        ts = 1000.0
        for i in range(10):
            t = ts + i * 0.5
            fusion.update_physio(_flow_physio(), t)
            fusion.update_kinematics(_flow_kinematics(), t)
            fusion.update_telemetry(_flow_telemetry(), t)
            vector, quality = fusion.fuse(t)
            scores = scorer.compute_scores(vector)
            estimate = smoother.update(scores, quality, t)

        # Context + prompt + parse
        ctx = _make_coding_context()
        messages = build_messages(ctx, estimate)
        plan = parse_and_validate(_valid_llm_json())

        elapsed = time.monotonic() - start
        assert elapsed < 0.200, f"Full pipeline: {elapsed*1000:.1f}ms (budget: 200ms)"
