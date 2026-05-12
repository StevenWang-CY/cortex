"""
Unit tests for Telemetry Engine — Input hooks, window tracker, feature aggregator.

Tests use synthetic event sequences to verify:
- Mouse velocity, jerk, and click burst computation
- Keyboard burst, interval variance, and backspace density
- Inactivity detection
- Window switch rate
- Scroll reversal detection
- Full TelemetryFeatures pipeline
"""

from __future__ import annotations

import numpy as np

from cortex.libs.config.settings import TelemetryConfig
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import (
    InputHooks,
    KeyEvent,
    KeyType,
    MouseButton,
    MouseClickEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    ScrollDirection,
)
from cortex.services.telemetry_engine.window_tracker import (
    WindowFocusEvent,
    WindowTracker,
)

# =============================================================================
# Helpers — Synthetic Event Generation
# =============================================================================


def make_linear_mouse_path(
    start_x: int = 100,
    start_y: int = 100,
    dx_per_step: int = 10,
    dy_per_step: int = 0,
    n_steps: int = 100,
    dt: float = 0.1,
    t0: float = 0.0,
) -> list[MouseMoveEvent]:
    """Create a linear mouse movement path."""
    events = []
    for i in range(n_steps):
        events.append(MouseMoveEvent(
            timestamp=t0 + i * dt,
            x=start_x + i * dx_per_step,
            y=start_y + i * dy_per_step,
        ))
    return events


def make_erratic_mouse_path(
    n_steps: int = 100,
    dt: float = 0.1,
    t0: float = 0.0,
    seed: int = 42,
) -> list[MouseMoveEvent]:
    """Create an erratic mouse movement path with random jumps."""
    rng = np.random.RandomState(seed)
    events = []
    x, y = 500, 500
    for i in range(n_steps):
        x += int(rng.uniform(-200, 200))
        y += int(rng.uniform(-200, 200))
        events.append(MouseMoveEvent(
            timestamp=t0 + i * dt,
            x=x, y=y,
        ))
    return events


def make_rapid_clicks(
    n_clicks: int = 10,
    interval: float = 0.1,
    t0: float = 0.0,
) -> list[MouseClickEvent]:
    """Create rapid click sequence."""
    events = []
    for i in range(n_clicks):
        t = t0 + i * interval
        events.append(MouseClickEvent(
            timestamp=t, x=300, y=300,
            button=MouseButton.LEFT, pressed=True,
        ))
        events.append(MouseClickEvent(
            timestamp=t + 0.02, x=300, y=300,
            button=MouseButton.LEFT, pressed=False,
        ))
    return events


def make_slow_clicks(
    n_clicks: int = 5,
    interval: float = 2.0,
    t0: float = 0.0,
) -> list[MouseClickEvent]:
    """Create slow click sequence."""
    events = []
    for i in range(n_clicks):
        t = t0 + i * interval
        events.append(MouseClickEvent(
            timestamp=t, x=300, y=300,
            button=MouseButton.LEFT, pressed=True,
        ))
        events.append(MouseClickEvent(
            timestamp=t + 0.05, x=300, y=300,
            button=MouseButton.LEFT, pressed=False,
        ))
    return events


def make_typing_sequence(
    n_keys: int = 50,
    interval: float = 0.12,
    backspace_rate: float = 0.0,
    t0: float = 0.0,
    seed: int = 42,
) -> list[KeyEvent]:
    """Create a typing sequence with configurable backspace rate."""
    rng = np.random.RandomState(seed)
    events = []
    for i in range(n_keys):
        t = t0 + i * interval
        if rng.random() < backspace_rate:
            key_type = KeyType.BACKSPACE
        else:
            key_type = KeyType.REGULAR
        events.append(KeyEvent(timestamp=t, key_type=key_type, pressed=True))
        events.append(KeyEvent(
            timestamp=t + 0.03, key_type=key_type, pressed=False,
        ))
    return events


def make_burst_typing(
    burst_size: int = 10,
    burst_interval: float = 0.05,
    pause: float = 2.0,
    n_bursts: int = 3,
    t0: float = 0.0,
) -> list[KeyEvent]:
    """Create typing with burst/pause pattern."""
    events = []
    t = t0
    for _ in range(n_bursts):
        for _ in range(burst_size):
            events.append(KeyEvent(
                timestamp=t, key_type=KeyType.REGULAR, pressed=True,
            ))
            events.append(KeyEvent(
                timestamp=t + 0.02, key_type=KeyType.REGULAR, pressed=False,
            ))
            t += burst_interval
        t += pause
    return events


def make_scroll_events(
    directions: list[str],
    dt: float = 0.3,
    t0: float = 0.0,
) -> list[MouseScrollEvent]:
    """Create scroll events from a direction sequence ('up' or 'down')."""
    events = []
    for i, d in enumerate(directions):
        dy = 3 if d == "up" else -3
        events.append(MouseScrollEvent(
            timestamp=t0 + i * dt,
            x=400, y=400,
            dx=0, dy=dy,
            direction=ScrollDirection.UP if d == "up" else ScrollDirection.DOWN,
        ))
    return events


def make_window_switches(
    apps: list[str],
    dt: float = 5.0,
    t0: float = 0.0,
) -> list[WindowFocusEvent]:
    """Create window focus events from an app name sequence."""
    events = []
    for i, app in enumerate(apps):
        events.append(WindowFocusEvent(
            timestamp=t0 + i * dt,
            app_name=app,
            window_title=f"{app} - Window",
        ))
    return events


# =============================================================================
# Input Hooks Tests
# =============================================================================


class TestInputHooks:
    """Test InputHooks event recording."""

    def test_record_mouse_move(self):
        hooks = InputHooks()
        hooks.record_mouse_move(100, 200, timestamp=1.0)
        hooks.record_mouse_move(110, 200, timestamp=1.1)

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert len(events["mouse_moves"]) == 2
        assert events["mouse_moves"][0].x == 100

    def test_record_mouse_click(self):
        hooks = InputHooks()
        hooks.record_mouse_click(300, 300, MouseButton.LEFT, True, timestamp=1.0)

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert len(events["mouse_clicks"]) == 1
        assert events["mouse_clicks"][0].button == MouseButton.LEFT

    def test_record_mouse_scroll(self):
        hooks = InputHooks()
        hooks.record_mouse_scroll(400, 400, 0, 3, timestamp=1.0)

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert len(events["mouse_scrolls"]) == 1
        assert events["mouse_scrolls"][0].direction == ScrollDirection.UP

    def test_record_scroll_down(self):
        hooks = InputHooks()
        hooks.record_mouse_scroll(400, 400, 0, -3, timestamp=1.0)

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert events["mouse_scrolls"][0].direction == ScrollDirection.DOWN

    def test_record_key_event(self):
        hooks = InputHooks()
        hooks.record_key_event(KeyType.REGULAR, True, timestamp=1.0)
        hooks.record_key_event(KeyType.BACKSPACE, True, timestamp=1.1)

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert len(events["key_events"]) == 2

    def test_window_filtering(self):
        """Events outside window should be excluded."""
        hooks = InputHooks()
        hooks.record_mouse_move(100, 200, timestamp=1.0)  # Outside window
        hooks.record_mouse_move(110, 200, timestamp=10.0)  # Inside window

        events = hooks.get_events_in_window(window_seconds=5.0, current_time=12.0)
        assert len(events["mouse_moves"]) == 1
        assert events["mouse_moves"][0].x == 110

    def test_reset_clears_buffers(self):
        hooks = InputHooks()
        hooks.record_mouse_move(100, 200, timestamp=1.0)
        hooks.record_key_event(KeyType.REGULAR, True, timestamp=1.0)
        hooks.reset()

        events = hooks.get_events_in_window(window_seconds=100.0, current_time=2.0)
        assert len(events["mouse_moves"]) == 0
        assert len(events["key_events"]) == 0


# =============================================================================
# Window Tracker Tests
# =============================================================================


class TestWindowTracker:
    """Test WindowTracker event recording."""

    def test_record_focus_event(self):
        tracker = WindowTracker()
        tracker.record_focus_event("VS Code", "main.py", timestamp=1.0)

        events = tracker.get_events_in_window(window_seconds=5.0, current_time=2.0)
        assert len(events) == 1
        assert events[0].app_name == "VS Code"

    def test_deduplicates_same_window(self):
        """Same window repeated should not create new events."""
        tracker = WindowTracker()
        tracker.record_focus_event("VS Code", "main.py", timestamp=1.0)
        tracker.record_focus_event("VS Code", "main.py", timestamp=2.0)  # Same

        events = tracker.get_events_in_window(window_seconds=5.0, current_time=3.0)
        assert len(events) == 1

    def test_records_different_windows(self):
        tracker = WindowTracker()
        tracker.record_focus_event("VS Code", "main.py", timestamp=1.0)
        tracker.record_focus_event("Chrome", "Google", timestamp=2.0)
        tracker.record_focus_event("Terminal", "bash", timestamp=3.0)

        events = tracker.get_events_in_window(window_seconds=5.0, current_time=4.0)
        assert len(events) == 3

    def test_window_filtering(self):
        tracker = WindowTracker()
        tracker.record_focus_event("Old App", "old", timestamp=1.0)
        tracker.record_focus_event("New App", "new", timestamp=10.0)

        events = tracker.get_events_in_window(window_seconds=5.0, current_time=12.0)
        assert len(events) == 1
        assert events[0].app_name == "New App"

    def test_reset(self):
        tracker = WindowTracker()
        tracker.record_focus_event("VS Code", "main.py", timestamp=1.0)
        tracker.reset()

        events = tracker.get_events_in_window(window_seconds=100.0, current_time=2.0)
        assert len(events) == 0


# =============================================================================
# Feature Aggregator — Mouse Features
# =============================================================================


class TestMouseFeatures:
    """Test mouse velocity, jerk, and click features."""

    def _make_aggregator(self, hooks: InputHooks | None = None) -> FeatureAggregator:
        return FeatureAggregator(
            input_hooks=hooks or InputHooks(),
            config=TelemetryConfig(window_seconds=15),
        )

    def test_linear_mouse_velocity(self):
        """Linear mouse movement should have consistent velocity and low variance."""
        moves = make_linear_mouse_path(dx_per_step=10, dt=0.1, n_steps=50)
        mean, var = FeatureAggregator._compute_mouse_velocity(moves)

        # Expected velocity: 10px / 0.1s = 100 px/s
        assert abs(mean - 100.0) < 5.0, f"Mean velocity={mean:.1f} expected ~100"
        assert var < 10.0, f"Variance={var:.1f} should be near 0 for linear motion"

    def test_no_mouse_events_zero_velocity(self):
        mean, var = FeatureAggregator._compute_mouse_velocity([])
        assert mean == 0.0
        assert var == 0.0

    def test_erratic_mouse_high_jerk(self):
        """Erratic mouse movement should have high jerk score."""
        moves = make_erratic_mouse_path(n_steps=50, dt=0.1)
        jerk = FeatureAggregator._compute_mouse_jerk(moves)
        assert jerk > 0.0, f"Erratic jerk={jerk:.3f} should be > 0"

    def test_linear_mouse_low_jerk(self):
        """Linear mouse movement should have low jerk."""
        moves = make_linear_mouse_path(dx_per_step=10, dt=0.1, n_steps=50)
        jerk = FeatureAggregator._compute_mouse_jerk(moves)
        assert jerk < 0.1, f"Linear jerk={jerk:.3f} should be near 0"

    def test_no_events_zero_jerk(self):
        jerk = FeatureAggregator._compute_mouse_jerk([])
        assert jerk == 0.0

    def test_rapid_clicks_high_burst_score(self):
        """Rapid clicks should produce high burst score."""
        clicks = make_rapid_clicks(n_clicks=10, interval=0.1)
        burst = FeatureAggregator._compute_click_burst_score(clicks)
        assert burst > 0.5, f"Rapid burst={burst:.3f} should be > 0.5"

    def test_slow_clicks_low_burst_score(self):
        """Slow clicks should produce low burst score."""
        clicks = make_slow_clicks(n_clicks=5, interval=2.0)
        burst = FeatureAggregator._compute_click_burst_score(clicks)
        assert burst < 0.2, f"Slow burst={burst:.3f} should be < 0.2"

    def test_click_frequency(self):
        """Click frequency should match expected clicks/second."""
        clicks = make_rapid_clicks(n_clicks=10, interval=0.5)
        freq = FeatureAggregator._compute_click_frequency(clicks, window_seconds=5.0)
        assert abs(freq - 2.0) < 0.5, f"Frequency={freq:.1f} expected ~2.0"

    def test_no_clicks_zero_frequency(self):
        freq = FeatureAggregator._compute_click_frequency([], window_seconds=15.0)
        assert freq == 0.0


# =============================================================================
# Feature Aggregator — Keyboard Features
# =============================================================================


class TestKeyboardFeatures:
    """Test keyboard burst, interval variance, and backspace density."""

    def test_burst_typing_high_burst_score(self):
        """Burst typing should produce high burst score."""
        keys = make_burst_typing(
            burst_size=10, burst_interval=0.05, pause=2.0, n_bursts=3,
        )
        burst = FeatureAggregator._compute_keyboard_burst_score(keys)
        assert burst > 0.3, f"Burst score={burst:.3f} should be > 0.3"

    def test_steady_typing_low_burst_score(self):
        """Steady, evenly-spaced typing should have low burst score."""
        keys = make_typing_sequence(n_keys=30, interval=0.5)
        burst = FeatureAggregator._compute_keyboard_burst_score(keys)
        assert burst < 0.2, f"Steady burst={burst:.3f} should be < 0.2"

    def test_steady_typing_low_interval_variance(self):
        """Steady typing should have low interval variance."""
        keys = make_typing_sequence(n_keys=30, interval=0.12)
        var = FeatureAggregator._compute_keystroke_interval_variance(keys)
        # Intervals all ~120ms → variance should be very low
        assert var < 100.0, f"Steady variance={var:.1f} should be < 100 ms^2"

    def test_erratic_typing_higher_variance(self):
        """Burst+pause typing should have higher interval variance."""
        keys = make_burst_typing(
            burst_size=5, burst_interval=0.05, pause=1.0, n_bursts=5,
        )
        var = FeatureAggregator._compute_keystroke_interval_variance(keys)
        # Mix of 50ms and 1000ms intervals → high variance
        assert var > 100.0, f"Erratic variance={var:.1f} should be > 100 ms^2"

    def test_no_backspace_zero_density(self):
        """Typing with no backspaces should have zero backspace density."""
        keys = make_typing_sequence(n_keys=20, backspace_rate=0.0)
        density = FeatureAggregator._compute_backspace_density(keys)
        assert density == 0.0

    def test_high_backspace_density(self):
        """50% backspace rate should produce ~0.5 density."""
        keys = make_typing_sequence(n_keys=100, backspace_rate=0.5, seed=123)
        density = FeatureAggregator._compute_backspace_density(keys)
        assert 0.3 < density < 0.7, f"Density={density:.3f} expected ~0.5"

    def test_no_keys_zero_density(self):
        density = FeatureAggregator._compute_backspace_density([])
        assert density == 0.0


# =============================================================================
# Feature Aggregator — Other Features
# =============================================================================


class TestOtherFeatures:
    """Test inactivity, window switch rate, scroll reversals."""

    def test_inactivity_with_recent_events(self):
        """Recent events should show low inactivity."""
        moves = [MouseMoveEvent(timestamp=9.8, x=100, y=100)]
        inactivity = FeatureAggregator._compute_inactivity(
            moves, [], [], [], current_time=10.0,
        )
        assert abs(inactivity - 0.2) < 0.01

    def test_inactivity_no_events(self):
        """No events should show full inactivity."""
        inactivity = FeatureAggregator._compute_inactivity(
            [], [], [], [], current_time=10.0,
        )
        assert inactivity == 10.0

    def test_window_switch_rate(self):
        """Window switches should be counted per minute."""
        events = make_window_switches(
            ["VS Code", "Chrome", "Terminal", "VS Code"], dt=5.0, t0=0.0,
        )
        rate = FeatureAggregator._compute_window_switch_rate(events, window_seconds=15.0)
        # 3 switches in 15s = 12 switches/min
        assert abs(rate - 12.0) < 1.0, f"Switch rate={rate:.1f} expected ~12"

    def test_no_window_events_zero_rate(self):
        rate = FeatureAggregator._compute_window_switch_rate([], window_seconds=15.0)
        assert rate == 0.0

    def test_scroll_reversal_high(self):
        """Alternating scroll directions should produce high reversal score."""
        scrolls = make_scroll_events(
            ["up", "down", "up", "down", "up", "down"],
        )
        score = FeatureAggregator._compute_scroll_reversal_score(scrolls)
        assert score == 1.0

    def test_scroll_reversal_none(self):
        """Consistent scroll direction should produce zero reversal."""
        scrolls = make_scroll_events(
            ["down", "down", "down", "down", "down"],
        )
        score = FeatureAggregator._compute_scroll_reversal_score(scrolls)
        assert score == 0.0

    def test_scroll_reversal_partial(self):
        """Some reversals should produce partial score."""
        scrolls = make_scroll_events(
            ["down", "down", "up", "down", "down"],  # 2/4 reversals
        )
        score = FeatureAggregator._compute_scroll_reversal_score(scrolls)
        assert abs(score - 0.5) < 0.01

    def test_no_scrolls_zero_reversal(self):
        score = FeatureAggregator._compute_scroll_reversal_score([])
        assert score == 0.0


# =============================================================================
# Feature Aggregator — Full Pipeline
# =============================================================================


class TestFeatureAggregatorPipeline:
    """Test the full build_features pipeline."""

    def _populate_hooks(
        self, hooks: InputHooks, t_start: float = 0.0
    ) -> float:
        """Populate hooks with synthetic events. Returns end time."""
        dt = 0.1
        t = t_start

        # Mouse moves (linear path)
        for i in range(50):
            hooks.record_mouse_move(100 + i * 5, 200, timestamp=t)
            t += dt

        # Some clicks
        for i in range(5):
            hooks.record_mouse_click(300, 300, timestamp=t)
            t += 0.3

        # Some keystrokes
        for i in range(30):
            hooks.record_key_event(KeyType.REGULAR, True, timestamp=t)
            hooks.record_key_event(KeyType.REGULAR, False, timestamp=t + 0.02)
            t += 0.15

        # A few backspaces
        for i in range(5):
            hooks.record_key_event(KeyType.BACKSPACE, True, timestamp=t)
            t += 0.15

        return t

    def test_build_features_returns_telemetry_features(self):
        """build_features should return valid TelemetryFeatures."""
        hooks = InputHooks()
        t_end = self._populate_hooks(hooks)

        aggregator = FeatureAggregator(
            input_hooks=hooks,
            config=TelemetryConfig(window_seconds=30),
        )
        features = aggregator.build_features(current_time=t_end)

        assert features.mouse_velocity_mean > 0.0
        assert features.click_frequency > 0.0
        assert features.backspace_density > 0.0
        assert features.inactivity_seconds >= 0.0

    def test_build_features_with_window_tracker(self):
        """build_features should include window switch rate."""
        hooks = InputHooks()
        tracker = WindowTracker()
        t_end = self._populate_hooks(hooks)

        # Add window switches
        tracker.record_focus_event("VS Code", "main.py", timestamp=1.0)
        tracker.record_focus_event("Chrome", "Google", timestamp=3.0)
        tracker.record_focus_event("Terminal", "bash", timestamp=5.0)

        aggregator = FeatureAggregator(
            input_hooks=hooks,
            window_tracker=tracker,
            config=TelemetryConfig(window_seconds=30),
        )
        features = aggregator.build_features(current_time=t_end)

        assert features.window_switch_rate > 0.0

    def test_build_features_with_tab_count(self):
        """build_features should include tab count from provider."""
        hooks = InputHooks()
        hooks.record_mouse_move(100, 200, timestamp=1.0)

        aggregator = FeatureAggregator(
            input_hooks=hooks,
            tab_count_provider=lambda: 12,
            config=TelemetryConfig(window_seconds=30),
        )
        features = aggregator.build_features(current_time=2.0)
        assert features.tab_count == 12

    def test_build_features_empty_events(self):
        """Empty events should produce zero/default features."""
        hooks = InputHooks()
        aggregator = FeatureAggregator(
            input_hooks=hooks,
            config=TelemetryConfig(window_seconds=15),
        )
        features = aggregator.build_features(current_time=10.0)

        assert features.mouse_velocity_mean == 0.0
        assert features.mouse_velocity_variance == 0.0
        assert features.mouse_jerk_score == 0.0
        assert features.click_burst_score == 0.0
        assert features.click_frequency == 0.0
        assert features.keyboard_burst_score == 0.0
        assert features.keystroke_interval_variance == 0.0
        assert features.backspace_density == 0.0
        assert features.window_switch_rate == 0.0

    def test_features_all_valid_ranges(self):
        """All feature values should be in their valid ranges."""
        hooks = InputHooks()
        self._populate_hooks(hooks)

        aggregator = FeatureAggregator(
            input_hooks=hooks,
            config=TelemetryConfig(window_seconds=30),
        )
        features = aggregator.build_features(current_time=20.0)

        assert features.mouse_velocity_mean >= 0.0
        assert features.mouse_velocity_variance >= 0.0
        assert 0.0 <= features.mouse_jerk_score <= 1.0
        assert 0.0 <= features.click_burst_score <= 1.0
        assert features.click_frequency >= 0.0
        assert 0.0 <= features.keyboard_burst_score <= 1.0
        assert features.keystroke_interval_variance >= 0.0
        assert 0.0 <= features.backspace_density <= 1.0
        assert features.inactivity_seconds >= 0.0
        assert features.window_switch_rate >= 0.0


# =============================================================================
# Integration: Module Imports
# =============================================================================


class TestTelemetryEngineImports:
    """Test that all telemetry engine exports are importable."""

    def test_import_input_hooks(self):
        from cortex.services.telemetry_engine import (
            InputHooks,
            MouseMoveEvent,
        )

        assert InputHooks is not None
        assert MouseMoveEvent is not None

    def test_import_window_tracker(self):
        from cortex.services.telemetry_engine import WindowFocusEvent, WindowTracker

        assert WindowTracker is not None
        assert WindowFocusEvent is not None

    def test_import_feature_aggregator(self):
        from cortex.services.telemetry_engine import FeatureAggregator

        assert FeatureAggregator is not None
