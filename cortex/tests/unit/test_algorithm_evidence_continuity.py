"""Hardware-free regressions for the October measurement/continuity audit."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.context import BrowserContext, EditorContext, TabInfo, TaskContext
from cortex.libs.schemas.features import FeatureName
from cortex.libs.schemas.state import SignalQuality, StateScores
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import ScoreSmoother
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import InputHooks, KeyType
from cortex.services.telemetry_engine.window_tracker import WindowTracker


class Listener:
    """Native listener double; no pynput import or TCC/device access."""
    IS_TRUSTED = True

    def __init__(self, *, fail: bool = False, **_kwargs: object) -> None:
        self.running = False
        self.alive = False
        self.fail = fail
        self.stopped = False

    def start(self) -> None:
        if self.fail:
            raise RuntimeError("native event tap denied")
        self.running = self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def stop(self) -> None:
        self.stopped = True
        self.running = self.alive = False

    def join(self, timeout: float) -> None:
        pass


@pytest.fixture
def observed_hooks(monkeypatch: pytest.MonkeyPatch) -> InputHooks:
    from cortex.services.telemetry_engine import input_hooks
    monkeypatch.setattr(input_hooks, "check_accessibility_permission", lambda: True)
    hooks = InputHooks()
    hooks._mouse_listener = Listener()
    hooks._keyboard_listener = Listener()
    hooks._mouse_listener.start()
    hooks._keyboard_listener.start()
    hooks._running = True
    return hooks


def test_never_started_input_cannot_become_under_engaged() -> None:
    hooks = InputHooks()
    aggregator, fusion, scorer = FeatureAggregator(hooks), FeatureFusion(), RuleScorer()
    for tick in range(601):
        now = tick * 0.5
        fusion.update_telemetry(aggregator.build_features(current_time=now), timestamp=now)
        vector, quality = fusion.fuse(timestamp=now)
        result = scorer.evaluate(vector)
        assert result.status != "estimated"
        assert result.scores.under_engaged == 0.0
        assert quality.telemetry == 0.0


def test_observed_silence_remains_evidence(observed_hooks: InputHooks) -> None:
    aggregator, fusion = FeatureAggregator(observed_hooks), FeatureFusion()
    for now in range(41):
        sample = aggregator.build_features(current_time=float(now))
        fusion.update_telemetry(sample, timestamp=float(now))
    vector, _ = fusion.fuse(timestamp=40.0)
    result = RuleScorer().evaluate(vector)
    assert sample.inactivity_seconds == 40.0
    assert vector.features[FeatureName.KEYPRESS_RATE_PER_MIN].valid
    assert result.scores.under_engaged > 0.0


@pytest.mark.parametrize("channel", ["mouse", "keyboard"])
def test_partial_listener_loss_abstains_without_fabricated_inactivity(
    observed_hooks: InputHooks, channel: str,
) -> None:
    aggregator, fusion = FeatureAggregator(observed_hooks), FeatureFusion()
    for now in range(41):
        aggregator.build_features(current_time=float(now))
    getattr(observed_hooks, f"_{channel}_listener").alive = False
    sample = aggregator.build_features(current_time=41.0)
    fusion.update_telemetry(sample, timestamp=41.0)
    vector, _ = fusion.fuse(timestamp=41.0)
    assert not vector.features[FeatureName.INACTIVITY_SECONDS].valid
    missing = FeatureName.CLICK_FREQUENCY if channel == "mouse" else FeatureName.KEYPRESS_RATE_PER_MIN
    available = FeatureName.KEYPRESS_RATE_PER_MIN if channel == "mouse" else FeatureName.CLICK_FREQUENCY
    assert not vector.features[missing].valid
    assert vector.features[available].valid
    getattr(observed_hooks, f"_{channel}_listener").alive = True
    assert aggregator.build_features(current_time=42.0).inactivity_seconds == 0.0
    assert aggregator.build_features(current_time=43.0).inactivity_seconds == 1.0


def test_revoked_keyboard_permission_is_unavailable(
    observed_hooks: InputHooks, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cortex.services.telemetry_engine import input_hooks
    assert observed_hooks.keyboard_source_available
    monkeypatch.setattr(input_hooks, "check_accessibility_permission", lambda: False)
    assert not observed_hooks.keyboard_source_available
    assert observed_hooks.mouse_source_available


def test_partial_start_failure_releases_started_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse, keyboard = Listener(), Listener(fail=True)
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace(
        mouse=SimpleNamespace(Listener=lambda **kwargs: mouse),
        keyboard=SimpleNamespace(Listener=lambda **kwargs: keyboard),
    ))
    hooks = InputHooks()
    assert not hooks.start()
    assert mouse.stopped and keyboard.stopped
    assert not mouse.alive
    assert not hooks.mouse_source_available


def test_stop_cleans_listeners_even_when_running_flag_is_false(observed_hooks: InputHooks) -> None:
    mouse, keyboard = observed_hooks._mouse_listener, observed_hooks._keyboard_listener
    observed_hooks._running = False
    observed_hooks.stop()
    assert mouse.stopped and keyboard.stopped


def test_acquisition_gap_does_not_count_as_inactivity(observed_hooks: InputHooks) -> None:
    aggregator = FeatureAggregator(observed_hooks)
    aggregator.build_features(current_time=0.0)
    assert aggregator.build_features(current_time=1.0).inactivity_seconds == 1.0
    assert aggregator.build_features(current_time=3600.0).inactivity_seconds == 0.0


@pytest.mark.parametrize("keys, expected", [
    ([KeyType.BACKSPACE, KeyType.BACKSPACE], False),
    ([KeyType.REGULAR, KeyType.REGULAR], False),
    ([KeyType.REGULAR, KeyType.REGULAR, KeyType.REGULAR], True),
    ([KeyType.REGULAR, KeyType.BACKSPACE, KeyType.REGULAR], False),
])
def test_typing_variance_requires_two_real_intervals(
    observed_hooks: InputHooks, keys: list[KeyType], expected: bool,
) -> None:
    for index, kind in enumerate(keys):
        observed_hooks.record_key_event(kind, timestamp=float(index))
    sample = FeatureAggregator(observed_hooks).build_features(current_time=3.0)
    fusion = FeatureFusion()
    fusion.update_telemetry(sample, timestamp=3.0)
    vector, _ = fusion.fuse(timestamp=3.0)
    feature = vector.features[FeatureName.KEYSTROKE_INTERVAL_VARIANCE]
    assert feature.valid is expected
    assert feature.value == (0.0 if expected else None)


def test_legacy_telemetry_decodes_but_cannot_assume_connected(observed_hooks: InputHooks) -> None:
    sample = FeatureAggregator(observed_hooks).build_features(current_time=0.0)
    legacy = sample.model_dump(exclude={"mouse_source_available", "keyboard_source_available", "keystroke_interval_count"})
    fusion = FeatureFusion()
    fusion.update_telemetry(type(sample).model_validate(legacy), timestamp=0.0)
    vector, quality = fusion.fuse(timestamp=0.0)
    assert quality.telemetry == 0.0
    assert not vector.features[FeatureName.INACTIVITY_SECONDS].valid


def test_gap_cannot_satisfy_entry_dwell() -> None:
    smoother = ScoreSmoother(StateConfig(ema_alpha=1.0))
    quality = SignalQuality(telemetry=1.0)
    scores = StateScores(hyper=0.99)
    smoother.update(scores, quality, timestamp=0.0)
    assert smoother.update(scores, quality, timestamp=3600.0).state == "UNKNOWN"
    for now in range(3601, 3631):
        result = smoother.update(scores, quality, timestamp=float(now))
    assert result.state == "HYPER"
    assert result.dwell_seconds == 0.0


def test_gap_revokes_committed_label_and_prevents_recovery() -> None:
    smoother = ScoreSmoother(StateConfig(ema_alpha=1.0, hyper_dwell_seconds=1))
    quality = SignalQuality(telemetry=1.0)
    scores = StateScores(hyper=0.99)
    smoother.update(scores, quality, timestamp=0.0)
    assert smoother.update(scores, quality, timestamp=1.0).state == "HYPER"
    result = smoother.update(StateScores(flow=0.9), quality, timestamp=3600.0)
    assert result.state == "UNKNOWN"
    assert result.dwell_seconds == 0.0
    assert result.scores.hyper == result.scores.recovery == 0.0
    assert smoother.transitions[-1].to_state == "UNKNOWN"


@pytest.mark.parametrize("replayed_time", [0.0, 1.0])
def test_replayed_observation_does_not_amplify_ema(replayed_time: float) -> None:
    smoother = ScoreSmoother()
    quality = SignalQuality(telemetry=1.0)
    original = smoother.update(StateScores(flow=0.7), quality, timestamp=1.0)
    for _ in range(20):
        replayed = smoother.update(StateScores(hyper=1.0), quality, timestamp=replayed_time)
    assert replayed.model_dump() == original.model_dump()


def _context(active: str = "vscode") -> TaskContext:
    return TaskContext(
        mode="mixed", active_app=active, complexity_score=0.5,
        current_goal_hint="implement parser",
        editor_context=EditorContext(file_path="parser.py", visible_range=(1, 20)),
        browser_context=BrowserContext(
            active_tab_title="Parser documentation", active_tab_url="https://example.com/parser",
            all_tabs=[TabInfo(title="Unrelated video", url="https://example.com/video")],
        ),
    )


@pytest.mark.parametrize("active_app", ["Code", "Google Chrome"])
def test_goal_context_uses_actual_nested_foreground_metadata(active_app: str) -> None:
    detector = RabbitHoleDetector(min_drift_minutes=0.01, cooldown_seconds=0)
    for now in range(10):
        assert detector.check_context(_context(), active_app=active_app, state="FLOW",
                                      current_time=float(now), may_trigger=True) is None
    assert not detector.is_drifting


def test_inactive_tabs_cannot_hide_active_goal_mismatch() -> None:
    context = _context("chrome")
    assert context.browser_context is not None
    context.browser_context.active_tab_title = "Unrelated video"
    context.browser_context.all_tabs = [TabInfo(title="Parser docs", url="https://example.com/parser")]
    detector = RabbitHoleDetector(min_drift_minutes=0.01, cooldown_seconds=0)
    detector.check_context(context, active_app="Google Chrome", state="FLOW", current_time=0, may_trigger=True)
    alert = detector.check_context(context, active_app="Google Chrome", state="FLOW", current_time=1, may_trigger=True)
    assert alert is not None
    assert "Unrelated video" in alert.summary


def test_missing_context_resets_goal_dwell() -> None:
    detector = RabbitHoleDetector(min_drift_minutes=1, cooldown_seconds=0)
    detector.check(goal="parser", current_file="video.ts", current_app="Code", current_time=0)
    assert detector.is_drifting
    assert detector.check(goal="parser", current_file="", current_app="Code", current_time=90) is None
    assert not detector.is_drifting
    assert detector.check(goal="parser", current_file="video.ts", current_app="Code", current_time=91) is None


def test_changed_goal_cannot_restore_previous_goals_file() -> None:
    detector = RabbitHoleDetector(min_drift_minutes=0, cooldown_seconds=0)
    detector.check(goal="parser", current_file="parser.py", current_app="Code", current_time=0)
    detector.check(goal="database", current_file="video.ts", current_app="Code", current_time=1)
    alert = detector.check(goal="database", current_file="video.ts", current_app="Code", current_time=2)
    assert alert is not None and alert.suggested_file is None


def test_reading_checkin_is_camera_invariant_and_requires_observed_input() -> None:
    outputs = []
    for blink in (None, 0.0, 17.0, 99.0):
        detector = ZombieReadingDetector(cooldown=0)
        outputs.append([detector.update("HYPO", 0.0, blink, "chrome", float(t)) for t in range(121)])
    assert all(output == outputs[0] for output in outputs)
    assert outputs[0][-1] and not any(outputs[0][:-1])
    missing = ZombieReadingDetector(cooldown=0)
    assert not any(missing.update("HYPO", None, 99.0, "chrome", float(t)) for t in range(500))


def test_stationary_foreground_does_not_expire_with_switch_window() -> None:
    from cortex.services.runtime_daemon import CortexDaemon
    clock = FakeClock()
    tracker = WindowTracker(clock=clock)
    tracker.record_focus_event("Code", "parser.py", timestamp=0.0)
    tracker._running = True  # A live, stationary tracker; no OS thread needed.
    clock.advance(monotonic_ns=600_000_000_000)
    assert tracker.get_events_in_window(60) == []
    daemon = SimpleNamespace(_window_tracker=tracker)
    assert CortexDaemon._current_app_name(daemon) == "Code"
    tracker._running = False
    assert CortexDaemon._current_app_name(daemon) == "other"


@pytest.mark.asyncio
async def test_runtime_sampler_publishes_unavailability_without_hardware() -> None:
    from cortex.services.runtime_daemon import CortexDaemon
    clock = FakeClock()
    fusion = FeatureFusion(clock=clock)
    daemon = SimpleNamespace(_aggregator=FeatureAggregator(InputHooks(clock=clock), clock=clock),
                             _feature_fusion=fusion, _services=Mock(), _runtime_data=Mock())
    for _ in range(100):
        await CortexDaemon._sample_telemetry(daemon)
        clock.advance(monotonic_ns=500_000_000)
    vector, quality = fusion.fuse()
    assert RuleScorer().evaluate(vector).status == "insufficient_evidence"
    assert quality.telemetry == 0.0


@pytest.mark.asyncio
async def test_runtime_special_call_sites_respect_camera_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    import cortex.services.runtime_daemon as module
    from cortex.services.runtime_daemon import CortexDaemon
    daemon = CortexDaemon()
    daemon._latest_context = _context()
    monkeypatch.setattr(daemon, "_record_state_estimate", Mock())
    monkeypatch.setattr(daemon, "_publish_state_to_local_subscribers", Mock())
    monkeypatch.setattr(daemon, "_dispatch_restore_updates", Mock())
    monkeypatch.setattr(daemon, "_maybe_trigger_leetcode_interventions", AsyncMock())
    monkeypatch.setattr(daemon, "_maybe_propose_policy_intervention", AsyncMock())
    monkeypatch.setattr(daemon, "_evaluate_auto_distraction_block", AsyncMock())
    monkeypatch.setattr(daemon, "_receptivity_inputs", lambda: SimpleNamespace(
        mic_active=False, fullscreen_active=False, typing_burst_seconds=0, within_work_hours=True))
    daemon._zombie_detector = Mock()
    daemon._zombie_detector.update.return_value = False
    daemon._rabbit_hole = Mock()
    daemon._rabbit_hole.check_context.return_value = None
    async def end_loop(_delay: float) -> None:
        raise asyncio.CancelledError
    monkeypatch.setattr(module.asyncio, "sleep", end_loop)
    await daemon._state_loop()
    daemon._zombie_detector.update.assert_called_once()
    assert daemon._zombie_detector.update.call_args.kwargs["blink_rate"] is None
    assert daemon._zombie_detector.update.call_args.kwargs["mouse_velocity"] is None
    daemon._rabbit_hole.check_context.assert_called_once()
    assert daemon._rabbit_hole.check_context.call_args.args == (daemon._latest_context,)


def test_support_identity_covers_measurement_and_temporal_behavior() -> None:
    from cortex.scripts.generate_support_model_identity import IMPLEMENTATION_COMPONENTS
    assert {
        "services/state_engine/smoother.py", "services/state_engine/feature_fusion.py",
        "services/telemetry_engine/input_hooks.py", "services/telemetry_engine/feature_aggregator.py",
        "services/telemetry_engine/focus_graph.py", "libs/schemas/features.py",
    } <= set(IMPLEMENTATION_COMPONENTS)
