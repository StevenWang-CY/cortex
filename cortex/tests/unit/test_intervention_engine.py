"""
Tests for Phase 11: Intervention Engine

Covers:
- Trigger evaluation (level selection, cooldown, dwell, quiet mode, adaptive thresholds)
- Workspace snapshot capture
- Plan validation (destructive actions, headline length, step count)
- hide_targets mapping to adapter commands
- Executor (apply, reverse, mutation tracking)
- Restore manager (timeout, recovery detection, dismissal, outcomes)
- Full cycle: trigger → snapshot → validate → execute → restore
- Module imports
"""

from __future__ import annotations

import unittest
from typing import Any

import pytest

from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.intervention import InterventionPlan, UIPlan, WorkspaceSnapshot
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.intervention_engine.executor import InterventionExecutor, Mutation
from cortex.services.intervention_engine.planner import (
    AdapterCommand,
    map_hide_targets,
    prepare_plan,
    validate_plan,
)
from cortex.services.intervention_engine.restore import ActiveIntervention, RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot
from cortex.services.intervention_engine.trigger import InterventionTrigger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_estimate(
    state: str = "HYPER",
    confidence: float = 0.90,
    dwell: float = 10.0,
    physio_q: float = 0.8,
    kinematics_q: float = 0.7,
    telemetry_q: float = 0.9,
) -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=confidence,
        scores=StateScores(flow=0.1, hypo=0.05, hyper=0.9, recovery=0.05),
        signal_quality=SignalQuality(
            physio=physio_q, kinematics=kinematics_q, telemetry=telemetry_q
        ),
        timestamp=1000.0,
        dwell_seconds=dwell,
    )


def _make_plan(
    level: str = "simplified_workspace",
    headline: str = "Fix the error first",
    steps: list[str] | None = None,
    hide_targets: list[str] | None = None,
    destructive: bool = False,
) -> InterventionPlan:
    from cortex.libs.schemas.intervention import SuggestedAction

    if steps is None:
        steps = ["Check line 10", "Fix the variable"]
    if hide_targets is None:
        hide_targets = ["editor_symbols_except_current_function"]

    suggested_actions = []
    if destructive:
        headline = "Delete unused files"
        steps = ["Delete config.yaml", "Remove permanently the backup"]
        # Build a SuggestedAction with a destructive action_type by
        # bypassing Pydantic validation (since delete_file is not a
        # valid Literal value in production, but is_destructive guards
        # against it as a safety net).
        action = SuggestedAction.model_construct(
            action_id="act_destructive",
            action_type="delete_file",
            tab_index=None,
            target="",
            label="Delete project files",
            reason="Remove clutter",
            category="recommended",
            reversible=False,
            group_id=None,
            metadata={},
        )
        suggested_actions = [action]

    return InterventionPlan(
        level=level,
        situation_summary="Test summary.",
        headline=headline,
        primary_focus="Test focus",
        micro_steps=steps,
        hide_targets=hide_targets,
        ui_plan=UIPlan(
            dim_background=True,
            show_overlay=True,
            fold_unrelated_code=True,
            intervention_type=level,
        ),
        tone="direct",
        suggested_actions=suggested_actions,
    )


def _make_context(
    with_editor: bool = True,
    with_browser: bool = True,
    with_terminal: bool = True,
) -> TaskContext:
    editor = None
    if with_editor:
        editor = EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 50),
            symbol_at_cursor="handle_request",
            diagnostics=[
                Diagnostic(severity="error", message="NameError", line=10),
            ],
        )

    browser = None
    if with_browser:
        browser = BrowserContext(
            active_tab_title="Docs",
            active_tab_url="https://docs.python.org",
            all_tabs=[
                TabInfo(title="Docs", url="https://docs.python.org", tab_type="documentation", is_active=True),
                TabInfo(title="SO", url="https://stackoverflow.com/q/1", tab_type="stackoverflow"),
                TabInfo(title="GitHub", url="https://github.com/test", tab_type="code_host"),
            ],
        )

    terminal = None
    if with_terminal:
        terminal = TerminalContext(
            last_n_lines=["$ python main.py", "NameError: x"],
            detected_errors=["NameError: x"],
        )

    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.75,
        editor_context=editor,
        terminal_context=terminal,
        browser_context=browser,
    )


class MockAdapter:
    """Simple mock workspace adapter for testing."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        self.calls.append((action, params))
        return not self._fail


# ===========================================================================
# Trigger Tests
# ===========================================================================


class TestInterventionTrigger(unittest.TestCase):
    """Test trigger evaluation logic."""

    def _make_trigger(self, **kwargs) -> InterventionTrigger:
        return InterventionTrigger(**kwargs)

    def test_triggers_on_hyper_with_high_confidence(self):
        trigger = self._make_trigger()
        est = _make_estimate(state="HYPER", confidence=0.90, dwell=10.0)
        decision = trigger.evaluate(est, complexity_score=0.8, current_time=100.0)
        assert decision.should_trigger is True
        assert decision.level is not None

    def test_no_trigger_on_flow(self):
        trigger = self._make_trigger()
        est = _make_estimate(state="FLOW", confidence=0.90)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.should_trigger is False
        assert "FLOW" in decision.reasons[0]

    def test_no_trigger_low_confidence(self):
        trigger = self._make_trigger(overlay_threshold=0.80)
        est = _make_estimate(confidence=0.60)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.should_trigger is False

    def test_no_trigger_low_dwell(self):
        trigger = self._make_trigger(dwell_seconds=8.0)
        est = _make_estimate(dwell=3.0)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.should_trigger is False

    def test_no_trigger_poor_signal(self):
        trigger = self._make_trigger()
        est = _make_estimate(physio_q=0.0, kinematics_q=0.0, telemetry_q=0.0)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.should_trigger is False

    def test_cooldown_prevents_retrigger(self):
        trigger = self._make_trigger(cooldown_seconds=60.0)
        est = _make_estimate()
        d1 = trigger.evaluate(est, current_time=100.0)
        assert d1.should_trigger is True
        d2 = trigger.evaluate(est, current_time=130.0)
        assert d2.should_trigger is False
        assert d2.cooldown_remaining > 0

    def test_cooldown_expires(self):
        trigger = self._make_trigger(cooldown_seconds=60.0)
        est = _make_estimate()
        trigger.evaluate(est, current_time=100.0)
        d2 = trigger.evaluate(est, current_time=200.0)
        assert d2.should_trigger is True

    def test_level_selection_overlay(self):
        trigger = self._make_trigger()
        est = _make_estimate(confidence=0.75)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.level == "overlay_only"

    def test_level_selection_simplified(self):
        trigger = self._make_trigger()
        est = _make_estimate(confidence=0.90)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.level == "simplified_workspace"

    def test_level_selection_guided(self):
        trigger = self._make_trigger()
        est = _make_estimate(confidence=0.97)
        decision = trigger.evaluate(est, current_time=100.0)
        assert decision.level == "guided_mode"

    def test_quiet_mode_on_repeated_dismissals(self):
        trigger = self._make_trigger(
            max_dismissals=3,
            dismissal_window_seconds=300.0,
            quiet_mode_seconds=1800.0,
        )
        for i in range(3):
            trigger.record_dismissal(timestamp=100.0 + i * 10.0)

        est = _make_estimate()
        decision = trigger.evaluate(est, current_time=150.0)
        assert decision.should_trigger is False
        assert decision.quiet_mode_active is True

    def test_quiet_mode_expires(self):
        trigger = self._make_trigger(
            max_dismissals=3, quiet_mode_seconds=60.0
        )
        for i in range(3):
            trigger.record_dismissal(timestamp=100.0 + i)

        est = _make_estimate()
        decision = trigger.evaluate(est, current_time=200.0)
        assert decision.quiet_mode_active is False
        assert decision.should_trigger is True

    def test_adaptive_threshold_bump(self):
        trigger = self._make_trigger(
            overlay_threshold=0.70,
            dismissal_bump=0.05,
            dismissal_decay_seconds=3600.0,
        )
        trigger.record_dismissal(timestamp=100.0)
        trigger.record_dismissal(timestamp=101.0)

        est = _make_estimate(confidence=0.75)
        # effective threshold = 0.70 + 2*0.05 = 0.80, confidence 0.75 < 0.80
        decision = trigger.evaluate(est, current_time=102.0)
        assert decision.should_trigger is False

    def test_reset_cooldown(self):
        trigger = self._make_trigger(cooldown_seconds=60.0)
        est = _make_estimate()
        trigger.evaluate(est, current_time=100.0)
        trigger.reset_cooldown()
        d2 = trigger.evaluate(est, current_time=105.0)
        assert d2.should_trigger is True


# ===========================================================================
# Snapshot Tests
# ===========================================================================


class TestSnapshot(unittest.TestCase):
    """Test workspace snapshot capture."""

    def test_empty_snapshot(self):
        snap = capture_snapshot(timestamp=100.0)
        assert isinstance(snap, WorkspaceSnapshot)
        assert snap.intervention_id.startswith("int_")
        assert snap.timestamp == 100.0

    def test_snapshot_with_editor(self):
        ctx = _make_context(with_editor=True, with_browser=False, with_terminal=False)
        snap = capture_snapshot(ctx, timestamp=100.0)
        assert snap.has_editor_state
        assert len(snap.fold_states) == 1
        assert snap.fold_states[0].file_path == "/src/main.py"

    def test_snapshot_with_browser(self):
        ctx = _make_context(with_editor=False, with_browser=True, with_terminal=False)
        snap = capture_snapshot(ctx, timestamp=100.0)
        assert snap.has_browser_state
        assert len(snap.tab_visibility) == 3
        assert snap.active_tab_id is not None

    def test_snapshot_with_terminal(self):
        ctx = _make_context(with_editor=False, with_browser=False, with_terminal=True)
        snap = capture_snapshot(ctx, timestamp=100.0)
        assert snap.terminal_scroll_position == 2

    def test_snapshot_custom_intervention_id(self):
        snap = capture_snapshot(intervention_id="int_custom123", timestamp=100.0)
        assert snap.intervention_id == "int_custom123"

    def test_snapshot_full_context(self):
        ctx = _make_context()
        snap = capture_snapshot(ctx, timestamp=100.0)
        assert snap.has_editor_state
        assert snap.has_browser_state
        assert snap.editor_visible_range == (1, 50)


# ===========================================================================
# Planner Validation Tests
# ===========================================================================


class TestPlanValidation(unittest.TestCase):
    """Test plan validation."""

    def test_valid_plan(self):
        plan = _make_plan()
        result = validate_plan(plan)
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_headline_too_long(self):
        """Headline with >15 words but <=100 chars should fail word-count check."""
        plan = _make_plan(headline="one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen")
        result = validate_plan(plan)
        assert result.is_valid is False
        assert any("headline" in e for e in result.errors)

    def test_pydantic_rejects_excess_steps(self):
        """Pydantic schema itself rejects >3 micro_steps."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            _make_plan(steps=["a", "b", "c", "d"])

    def test_no_steps(self):
        # InterventionPlan schema requires min_length=1, so we test via validate_plan
        plan = _make_plan(steps=["single step"])
        result = validate_plan(plan)
        assert result.is_valid is True

    def test_destructive_plan_rejected(self):
        plan = _make_plan(destructive=True)
        result = validate_plan(plan)
        # v0.2.0: destructive actions are downgraded to warnings so the
        # remainder of the plan can still execute safely.
        assert result.is_valid is True
        assert any("destructive" in w for w in result.warnings)

    def test_unknown_hide_target_warning(self):
        plan = _make_plan(hide_targets=["unknown_target"])
        result = validate_plan(plan)
        assert result.is_valid is True  # warning, not error
        assert len(result.warnings) > 0

    def test_no_hide_targets_warning(self):
        plan = _make_plan(hide_targets=[])
        result = validate_plan(plan)
        assert result.is_valid is True
        assert any("no hide_targets" in w for w in result.warnings)


class TestHideTargetMapping(unittest.TestCase):
    """Test mapping of hide_targets to adapter commands."""

    def test_maps_known_targets(self):
        plan = _make_plan(hide_targets=[
            "browser_tabs_except_active",
            "editor_symbols_except_current_function",
        ])
        commands = map_hide_targets(plan)
        adapters = [c.adapter for c in commands]
        assert "browser" in adapters
        assert "editor" in adapters

    def test_skips_unknown_targets(self):
        plan = _make_plan(hide_targets=["nonexistent_target"])
        commands = map_hide_targets(plan)
        # Should still have ui_plan commands (dim, overlay, fold)
        assert len(commands) > 0

    def test_ui_plan_commands_added(self):
        plan = _make_plan(hide_targets=[])
        commands = map_hide_targets(plan)
        actions = [c.action for c in commands]
        assert "dim_background" in actions
        assert "show_overlay" in actions
        # fold_unrelated_code from ui_plan
        assert "fold_except_current" in actions

    def test_no_duplicate_fold(self):
        """editor_symbols_except_current_function + fold_unrelated_code shouldn't duplicate."""
        plan = _make_plan(hide_targets=["editor_symbols_except_current_function"])
        commands = map_hide_targets(plan)
        fold_commands = [c for c in commands if c.action == "fold_except_current"]
        assert len(fold_commands) == 1

    def test_prepare_plan_valid(self):
        plan = _make_plan()
        result, commands = prepare_plan(plan)
        assert result.is_valid is True
        assert len(commands) > 0

    def test_prepare_plan_invalid(self):
        plan = _make_plan(destructive=True)
        result, commands = prepare_plan(plan)
        assert result.is_valid is True
        assert len(result.warnings) > 0
        assert len(commands) > 0


# ===========================================================================
# Executor Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_executor_apply_commands():
    executor = InterventionExecutor()
    adapter = MockAdapter()
    executor.register_adapter("editor", adapter)
    executor.register_adapter("overlay", MockAdapter())

    plan = _make_plan()
    commands = map_hide_targets(plan)
    mutations = await executor.apply(plan, commands, timestamp=100.0)

    assert len(mutations) > 0
    successful = [m for m in mutations if m.success]
    assert len(successful) > 0


@pytest.mark.asyncio
async def test_executor_missing_adapter():
    executor = InterventionExecutor()
    # No adapters registered
    plan = _make_plan()
    commands = [AdapterCommand(adapter="browser", action="hide_tabs")]
    mutations = await executor.apply(plan, commands)
    assert len(mutations) == 1
    assert mutations[0].success is False


@pytest.mark.asyncio
async def test_executor_failing_adapter():
    executor = InterventionExecutor()
    executor.register_adapter("editor", MockAdapter(fail=True))

    plan = _make_plan()
    commands = [AdapterCommand(adapter="editor", action="fold_except_current")]
    mutations = await executor.apply(plan, commands)
    assert mutations[0].success is False


@pytest.mark.asyncio
async def test_executor_reverse():
    executor = InterventionExecutor()
    adapter = MockAdapter()
    executor.register_adapter("editor", adapter)
    executor.register_adapter("overlay", MockAdapter())

    plan = _make_plan()
    commands = map_hide_targets(plan)
    await executor.apply(plan, commands)

    reversals = await executor.reverse(plan.intervention_id)
    assert len(reversals) > 0
    assert all(r.success for r in reversals)


@pytest.mark.asyncio
async def test_executor_active_interventions():
    executor = InterventionExecutor()
    executor.register_adapter("editor", MockAdapter())

    plan = _make_plan()
    commands = [AdapterCommand(adapter="editor", action="fold_except_current")]
    await executor.apply(plan, commands)

    assert plan.intervention_id in executor.active_intervention_ids
    assert len(executor.get_active_mutations(plan.intervention_id)) == 1


@pytest.mark.asyncio
async def test_executor_reverse_clears_active():
    executor = InterventionExecutor()
    executor.register_adapter("editor", MockAdapter())

    plan = _make_plan()
    commands = [AdapterCommand(adapter="editor", action="fold_except_current")]
    await executor.apply(plan, commands)
    await executor.reverse(plan.intervention_id)

    assert plan.intervention_id not in executor.active_intervention_ids


# ===========================================================================
# Restore Manager Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_restore_timeout():
    """Intervention should auto-end after timeout."""
    executor = InterventionExecutor()
    executor.register_adapter("editor", MockAdapter())
    manager = RestoreManager(executor, timeout_seconds=60.0)

    snap = WorkspaceSnapshot(intervention_id="int_test1", timestamp=100.0)
    manager.start_intervention("int_test1", snap, started_at=100.0)

    est = _make_estimate(state="HYPER")
    # Before timeout
    outcomes = await manager.update(est, current_time=150.0)
    assert len(outcomes) == 0

    # After timeout
    outcomes = await manager.update(est, current_time=200.0)
    assert len(outcomes) == 1
    assert outcomes[0].user_action == "timed_out"


@pytest.mark.asyncio
async def test_restore_natural_recovery():
    """Intervention should end on sustained FLOW."""
    manager = RestoreManager(
        timeout_seconds=300.0,
        recovery_threshold=0.70,
        recovery_dwell_seconds=15.0,
    )

    snap = WorkspaceSnapshot(intervention_id="int_test2", timestamp=100.0)
    manager.start_intervention("int_test2", snap, started_at=100.0)

    # FLOW starts at t=110
    flow_est = _make_estimate(state="FLOW", confidence=0.80)
    outcomes = await manager.update(flow_est, current_time=110.0)
    assert len(outcomes) == 0

    # FLOW sustained for 15s → recovery at t=126
    outcomes = await manager.update(flow_est, current_time=126.0)
    assert len(outcomes) == 1
    assert outcomes[0].user_action == "natural_recovery"
    assert outcomes[0].recovery_detected is True


@pytest.mark.asyncio
async def test_restore_recovery_reset_on_state_change():
    """Recovery timer should reset if state leaves FLOW."""
    manager = RestoreManager(
        timeout_seconds=300.0,
        recovery_dwell_seconds=15.0,
    )

    snap = WorkspaceSnapshot(intervention_id="int_test3", timestamp=100.0)
    manager.start_intervention("int_test3", snap, started_at=100.0)

    flow_est = _make_estimate(state="FLOW", confidence=0.80)
    await manager.update(flow_est, current_time=110.0)

    # Back to HYPER at t=120 (only 10s of FLOW)
    hyper_est = _make_estimate(state="HYPER")
    outcomes = await manager.update(hyper_est, current_time=120.0)
    assert len(outcomes) == 0

    # FLOW again at t=130
    await manager.update(flow_est, current_time=130.0)

    # Not enough dwell at t=140 (only 10s)
    outcomes = await manager.update(flow_est, current_time=140.0)
    assert len(outcomes) == 0

    # Now enough at t=146 (16s of sustained FLOW)
    outcomes = await manager.update(flow_est, current_time=146.0)
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_restore_dismiss():
    """User dismissal should end intervention."""
    manager = RestoreManager(timeout_seconds=300.0)
    snap = WorkspaceSnapshot(intervention_id="int_test4", timestamp=100.0)
    manager.start_intervention("int_test4", snap, started_at=100.0)

    outcome = await manager.dismiss("int_test4", current_time=120.0)
    assert outcome is not None
    assert outcome.user_action == "dismissed"
    assert outcome.duration_seconds == pytest.approx(20.0, abs=0.1)
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_restore_engage():
    """User engagement should end intervention positively."""
    manager = RestoreManager(timeout_seconds=300.0)
    snap = WorkspaceSnapshot(intervention_id="int_test5", timestamp=100.0)
    manager.start_intervention("int_test5", snap, started_at=100.0)

    outcome = await manager.engage("int_test5", current_time=130.0)
    assert outcome is not None
    assert outcome.user_action == "engaged"
    assert outcome.recovery_detected is True


@pytest.mark.asyncio
async def test_restore_dismiss_unknown():
    manager = RestoreManager()
    outcome = await manager.dismiss("nonexistent")
    assert outcome is None


@pytest.mark.asyncio
async def test_restore_outcomes_tracked():
    manager = RestoreManager(timeout_seconds=10.0)
    snap = WorkspaceSnapshot(intervention_id="int_test6", timestamp=100.0)
    manager.start_intervention("int_test6", snap, started_at=100.0)

    est = _make_estimate()
    await manager.update(est, current_time=200.0)  # timeout

    assert len(manager.outcomes) == 1
    assert manager.outcomes[0].intervention_id == "int_test6"


# ===========================================================================
# Full Cycle Test
# ===========================================================================


@pytest.mark.asyncio
async def test_full_intervention_cycle():
    """Test complete cycle: trigger → snapshot → validate → execute → restore."""
    # 1. Trigger
    trigger = InterventionTrigger()
    est = _make_estimate(state="HYPER", confidence=0.90, dwell=10.0)
    decision = trigger.evaluate(est, complexity_score=0.8, current_time=100.0)
    assert decision.should_trigger is True
    assert decision.level == "simplified_workspace"

    # 2. Snapshot
    ctx = _make_context()
    snap = capture_snapshot(ctx, timestamp=100.0)
    assert snap.has_editor_state
    assert snap.has_browser_state

    # 3. Build and validate plan
    plan = _make_plan(level=decision.level)
    result, commands = prepare_plan(plan)
    assert result.is_valid is True
    assert len(commands) > 0

    # 4. Execute
    executor = InterventionExecutor()
    editor_adapter = MockAdapter()
    overlay_adapter = MockAdapter()
    executor.register_adapter("editor", editor_adapter)
    executor.register_adapter("overlay", overlay_adapter)

    mutations = await executor.apply(plan, commands, timestamp=100.0)
    successful = [m for m in mutations if m.success]
    assert len(successful) > 0

    # 5. Restore via manager
    manager = RestoreManager(
        executor,
        timeout_seconds=300.0,
        recovery_dwell_seconds=15.0,
    )
    manager.start_intervention(plan.intervention_id, snap, started_at=100.0)

    # User recovers
    flow_est = _make_estimate(state="FLOW", confidence=0.80)
    await manager.update(flow_est, current_time=200.0)
    outcomes = await manager.update(flow_est, current_time=216.0)
    assert len(outcomes) == 1
    assert outcomes[0].user_action == "natural_recovery"
    assert outcomes[0].workspace_restored is True


# ===========================================================================
# ActiveIntervention Tests
# ===========================================================================


class TestActiveIntervention(unittest.TestCase):
    """Test ActiveIntervention data class."""

    def test_timed_out_at(self):
        active = ActiveIntervention(
            intervention_id="test",
            snapshot=WorkspaceSnapshot(intervention_id="test", timestamp=100.0),
            started_at=100.0,
            timeout_seconds=60.0,
        )
        assert active.timed_out_at(150.0) is False
        assert active.timed_out_at(170.0) is True

    def test_duration_at(self):
        active = ActiveIntervention(
            intervention_id="test",
            snapshot=WorkspaceSnapshot(intervention_id="test", timestamp=100.0),
            started_at=100.0,
        )
        assert active.duration_at(150.0) == pytest.approx(50.0)

    def test_check_recovery_sustained(self):
        active = ActiveIntervention(
            intervention_id="test",
            snapshot=WorkspaceSnapshot(intervention_id="test", timestamp=100.0),
            started_at=100.0,
            recovery_threshold=0.70,
            recovery_dwell_seconds=15.0,
        )
        flow = _make_estimate(state="FLOW", confidence=0.80)
        assert active.check_recovery(flow, 110.0) is False  # start tracking
        assert active.check_recovery(flow, 120.0) is False  # 10s not enough
        assert active.check_recovery(flow, 126.0) is True   # 16s enough

    def test_check_recovery_resets(self):
        active = ActiveIntervention(
            intervention_id="test",
            snapshot=WorkspaceSnapshot(intervention_id="test", timestamp=100.0),
            started_at=100.0,
            recovery_dwell_seconds=15.0,
        )
        flow = _make_estimate(state="FLOW", confidence=0.80)
        hyper = _make_estimate(state="HYPER")

        active.check_recovery(flow, 110.0)
        active.check_recovery(hyper, 115.0)  # resets
        assert active.flow_start_time is None
        assert active.check_recovery(flow, 120.0) is False  # restart


# ===========================================================================
# Mutation Tests
# ===========================================================================


class TestMutation(unittest.TestCase):
    """Test Mutation data class."""

    def test_reversible(self):
        m = Mutation(adapter="editor", action="fold", reverse_action="unfold")
        assert m.is_reversible is True

    def test_not_reversible(self):
        m = Mutation(adapter="editor", action="custom")
        assert m.is_reversible is False


# ===========================================================================
# is_destructive Tests (W-11 fix: action_type not label text)
# ===========================================================================


class TestIsDestructive(unittest.TestCase):
    """Test is_destructive uses action_type checking, not label substring matching."""

    def test_close_tab_is_not_destructive(self):
        """close_tab action is reversible, not destructive."""
        from cortex.libs.schemas.intervention import SuggestedAction
        plan = _make_plan()
        plan.suggested_actions = [
            SuggestedAction(
                action_type="close_tab",
                tab_index=0,
                label="Close New Tab",
                reason="Unrelated",
            ),
        ]
        assert plan.is_destructive is False

    def test_new_tab_label_not_destructive(self):
        """A tab titled 'New Tab' with close_tab action should NOT trigger is_destructive."""
        from cortex.libs.schemas.intervention import SuggestedAction
        plan = _make_plan(headline="Simplify your workspace")
        plan.suggested_actions = [
            SuggestedAction(
                action_type="close_tab",
                tab_index=2,
                label="Close New Tab",
                reason="Empty tab",
                metadata={"tab_title": "New Tab"},
            ),
        ]
        assert plan.is_destructive is False

    def test_group_tabs_not_destructive(self):
        """group_tabs is not destructive."""
        from cortex.libs.schemas.intervention import SuggestedAction
        plan = _make_plan()
        plan.suggested_actions = [
            SuggestedAction(
                action_type="group_tabs",
                label="Group related tabs",
                reason="Reduce visual clutter",
            ),
        ]
        assert plan.is_destructive is False

    def test_no_actions_not_destructive(self):
        """Plan with no suggested_actions is not destructive."""
        plan = _make_plan()
        plan.suggested_actions = []
        assert plan.is_destructive is False

    def test_close_tab_reversible_tracked(self):
        """close_tab action should have reversible=True by default."""
        from cortex.libs.schemas.intervention import SuggestedAction
        action = SuggestedAction(
            action_type="close_tab",
            tab_index=1,
            label="Close Stack Overflow",
        )
        assert action.reversible is True


# ===========================================================================
# Staleness Check Tests (C-06: suppress stale interventions)
# ===========================================================================


class TestStalenessCheck(unittest.TestCase):
    """Test the staleness suppression logic used in runtime_daemon._trigger_intervention."""

    def _should_suppress_stale(
        self,
        current_state: StateEstimate,
        plan: InterventionPlan,
        context: TaskContext,
    ) -> bool:
        """Replicate the staleness check from runtime_daemon._trigger_intervention."""
        # Suppress only if student is in FLOW for >3s (genuine recovery)
        if (current_state.state == "FLOW"
                and current_state.dwell_seconds >= 3.0):
            return True
        # Also check if workspace context changed significantly
        if hasattr(context, 'browser_context') and context.browser_context:
            current_tab_count = len(context.browser_context.all_tabs) if context.browser_context.all_tabs else 0
            if plan.suggested_actions:
                stale_actions = sum(1 for a in plan.suggested_actions
                                    if a.tab_index is not None and a.tab_index >= current_tab_count)
                if stale_actions > len(plan.suggested_actions) * 0.5:
                    return True
        return False

    def test_suppress_when_flow_3s(self):
        """Student in FLOW for >=3s: intervention should be suppressed."""
        state = _make_estimate(state="FLOW", confidence=0.8, dwell=5.0)
        plan = _make_plan()
        ctx = _make_context()
        assert self._should_suppress_stale(state, plan, ctx) is True

    def test_deliver_when_recovery(self):
        """Student in RECOVERY: intervention should NOT be suppressed."""
        state = _make_estimate(state="RECOVERY", confidence=0.8, dwell=5.0)
        plan = _make_plan()
        ctx = _make_context()
        assert self._should_suppress_stale(state, plan, ctx) is False

    def test_deliver_when_flow_under_3s(self):
        """Student in FLOW for <3s: intervention should NOT be suppressed (not genuine recovery)."""
        state = _make_estimate(state="FLOW", confidence=0.8, dwell=2.0)
        plan = _make_plan()
        ctx = _make_context()
        assert self._should_suppress_stale(state, plan, ctx) is False

    def test_suppress_when_tab_references_invalid(self):
        """If >50% of action tab references are invalid, suppress."""
        from cortex.libs.schemas.intervention import SuggestedAction
        state = _make_estimate(state="HYPER", dwell=1.0)
        plan = _make_plan()
        # Context has 3 tabs (indices 0,1,2), but actions reference tab indices 5,6,7
        plan.suggested_actions = [
            SuggestedAction(action_type="close_tab", tab_index=5, label="Close tab 5"),
            SuggestedAction(action_type="close_tab", tab_index=6, label="Close tab 6"),
            SuggestedAction(action_type="highlight_tab", tab_index=0, label="Focus"),
        ]
        ctx = _make_context(with_browser=True)  # 3 tabs
        assert self._should_suppress_stale(state, plan, ctx) is True

    def test_deliver_when_tab_references_valid(self):
        """If tab references are valid, deliver."""
        from cortex.libs.schemas.intervention import SuggestedAction
        state = _make_estimate(state="HYPER", dwell=1.0)
        plan = _make_plan()
        plan.suggested_actions = [
            SuggestedAction(action_type="close_tab", tab_index=1, label="Close tab 1"),
            SuggestedAction(action_type="highlight_tab", tab_index=0, label="Focus"),
        ]
        ctx = _make_context(with_browser=True)  # 3 tabs
        assert self._should_suppress_stale(state, plan, ctx) is False


# ===========================================================================
# Import Tests
# ===========================================================================


class TestImports(unittest.TestCase):
    """Verify all public exports are importable."""

    def test_import_trigger(self):
        from cortex.services.intervention_engine import InterventionTrigger, TriggerDecision
        assert InterventionTrigger is not None
        assert TriggerDecision is not None

    def test_import_snapshot(self):
        from cortex.services.intervention_engine import capture_snapshot
        assert callable(capture_snapshot)

    def test_import_planner(self):
        from cortex.services.intervention_engine import (
            validate_plan,
        )
        assert callable(validate_plan)

    def test_import_executor(self):
        from cortex.services.intervention_engine import InterventionExecutor
        assert InterventionExecutor is not None

    def test_import_restore(self):
        from cortex.services.intervention_engine import RestoreManager
        assert RestoreManager is not None


if __name__ == "__main__":
    unittest.main()
