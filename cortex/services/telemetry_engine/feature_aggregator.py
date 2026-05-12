"""
Telemetry Engine — Feature Aggregator

Consumes raw input events (mouse moves, clicks, scrolls, keystrokes,
window focus changes) over a configurable window and computes all
derived TelemetryFeatures.

Derived features:
- mouse_velocity_mean / mouse_velocity_variance: mouse speed statistics
- mouse_jerk_score: acceleration variance (erratic movement, 0-1)
- click_burst_score: repeated rapid clicks (0-1)
- click_frequency: clicks per second
- keyboard_burst_score: typing intensity spikes (0-1)
- keystroke_interval_variance: typing rhythm regularity (ms^2)
- backspace_density: deletion-to-keystroke ratio (0-1)
- inactivity_seconds: time since last input event
- window_switch_rate: app/window switches per minute
- tab_count: browser tabs (from external source, optional)
- scroll_reversal_score: scroll direction changes (0-1)
"""

from __future__ import annotations

import logging
import time

import numpy as np

from cortex.libs.config.settings import TelemetryConfig
from cortex.libs.schemas.features import TelemetryFeatures
from cortex.services.telemetry_engine.focus_graph import FocusGraphBuilder
from cortex.services.telemetry_engine.input_hooks import (
    InputHooks,
    KeyEvent,
    KeyType,
    MouseClickEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    ScrollDirection,
)
from cortex.services.telemetry_engine.window_tracker import WindowFocusEvent, WindowTracker

logger = logging.getLogger(__name__)

# Click burst detection: clicks within this interval count as burst
_CLICK_BURST_INTERVAL_S = 0.3

# Keyboard burst detection: keystrokes within this interval count as burst
_KEY_BURST_INTERVAL_S = 0.1

# Maximum expected mouse velocity for normalization (px/s)
_MAX_MOUSE_VELOCITY = 5000.0

# Maximum expected jerk for normalization
_MAX_JERK_VARIANCE = 1e8


class FeatureAggregator:
    """
    Computes TelemetryFeatures from raw input events.

    Pulls events from InputHooks and WindowTracker, then computes
    all derived features over a configurable time window.

    Usage:
        hooks = InputHooks()
        tracker = WindowTracker()
        aggregator = FeatureAggregator(hooks, tracker)

        hooks.start()
        tracker.start()
        # ... events accumulate ...
        features = aggregator.build_features()
    """

    def __init__(
        self,
        input_hooks: InputHooks,
        window_tracker: WindowTracker | None = None,
        config: TelemetryConfig | None = None,
        tab_count_provider: callable | None = None,
    ) -> None:
        self._hooks = input_hooks
        self._window_tracker = window_tracker
        self._config = config or TelemetryConfig()
        self._tab_count_provider = tab_count_provider
        self._focus_graph = FocusGraphBuilder()

    def build_features(
        self,
        window_seconds: float | None = None,
        current_time: float | None = None,
    ) -> TelemetryFeatures:
        """
        Compute all TelemetryFeatures from events in the specified window.

        Args:
            window_seconds: Time window to aggregate over. Defaults to config.
            current_time: Reference time. Defaults to now.

        Returns:
            TelemetryFeatures Pydantic model with all computed features.
        """
        window = window_seconds or self._config.window_seconds
        now = current_time or time.monotonic()

        # Gather events
        events = self._hooks.get_events_in_window(window, now)
        mouse_moves: list[MouseMoveEvent] = events["mouse_moves"]
        mouse_clicks: list[MouseClickEvent] = events["mouse_clicks"]
        mouse_scrolls: list[MouseScrollEvent] = events["mouse_scrolls"]
        key_events: list[KeyEvent] = events["key_events"]

        # Window focus events
        window_events: list[WindowFocusEvent] = []
        if self._window_tracker is not None:
            window_events = self._window_tracker.get_events_in_window(window, now)

        # Feed window events to focus graph for thrashing detection
        for we in window_events:
            self._focus_graph.add_event(
                app_name=we.app_name,
                window_title=we.window_title,
                timestamp=we.timestamp,
            )

        # Compute all features
        vel_mean, vel_var = self._compute_mouse_velocity(mouse_moves)
        jerk_score = self._compute_mouse_jerk(mouse_moves)
        click_burst = self._compute_click_burst_score(mouse_clicks)
        click_freq = self._compute_click_frequency(mouse_clicks, window)
        kb_burst = self._compute_keyboard_burst_score(key_events)
        ks_variance = self._compute_keystroke_interval_variance(key_events)
        bs_density = self._compute_backspace_density(key_events)
        correction_rate = self._compute_correction_rate_per_100_keys(key_events)
        inactivity = self._compute_inactivity(
            mouse_moves, mouse_clicks, mouse_scrolls, key_events, now
        )
        switch_rate = self._compute_window_switch_rate(window_events, window)
        scroll_rev = self._compute_scroll_reversal_score(mouse_scrolls)
        scroll_back_rate = self._compute_scroll_back_rate_per_min(mouse_scrolls, window)
        thrashing = self._focus_graph.compute_thrashing_score(current_time=now)

        # Tab count from external provider
        tab_count = None
        if self._tab_count_provider is not None:
            try:
                tab_count = self._tab_count_provider()
            except Exception:
                pass

        self._latest_thrashing_score = thrashing

        return TelemetryFeatures(
            mouse_velocity_mean=vel_mean,
            mouse_velocity_variance=vel_var,
            mouse_jerk_score=jerk_score,
            click_burst_score=click_burst,
            click_frequency=click_freq,
            keyboard_burst_score=kb_burst,
            keystroke_interval_variance=ks_variance,
            backspace_density=bs_density,
            correction_rate_per_100_keys=correction_rate,
            inactivity_seconds=inactivity,
            window_switch_rate=switch_rate,
            tab_count=tab_count,
            scroll_reversal_score=scroll_rev,
            scroll_back_rate_per_min=scroll_back_rate,
        )

    @property
    def thrashing_score(self) -> float:
        """Get the latest thrashing score from focus graph analysis."""
        return getattr(self, '_latest_thrashing_score', 0.0)

    @property
    def focus_graph(self) -> FocusGraphBuilder:
        """Access the focus graph builder."""
        return self._focus_graph

    @staticmethod
    def _compute_mouse_velocity(
        moves: list[MouseMoveEvent],
    ) -> tuple[float, float]:
        """
        Compute mean and variance of mouse velocity (px/s).

        Returns:
            (mean_velocity, velocity_variance)
        """
        if len(moves) < 2:
            return 0.0, 0.0

        velocities = []
        for i in range(1, len(moves)):
            dt = moves[i].timestamp - moves[i - 1].timestamp
            if dt < 1e-6:
                continue

            dx = moves[i].x - moves[i - 1].x
            dy = moves[i].y - moves[i - 1].y
            dist = np.sqrt(dx**2 + dy**2)
            vel = dist / dt
            velocities.append(vel)

        if not velocities:
            return 0.0, 0.0

        vel_arr = np.array(velocities)
        return float(np.mean(vel_arr)), float(np.var(vel_arr))

    @staticmethod
    def _compute_mouse_jerk(moves: list[MouseMoveEvent]) -> float:
        """
        Compute mouse jerk score (acceleration variance, 0-1).

        Jerk is the derivative of acceleration. High jerk variance
        indicates erratic, non-smooth mouse movement.
        """
        if len(moves) < 3:
            return 0.0

        # Compute velocities
        velocities = []
        times = []
        for i in range(1, len(moves)):
            dt = moves[i].timestamp - moves[i - 1].timestamp
            if dt < 1e-6:
                continue

            dx = moves[i].x - moves[i - 1].x
            dy = moves[i].y - moves[i - 1].y
            vx = dx / dt
            vy = dy / dt
            velocities.append((vx, vy))
            times.append(moves[i].timestamp)

        if len(velocities) < 2:
            return 0.0

        # Compute accelerations
        accelerations = []
        for i in range(1, len(velocities)):
            dt = times[i] - times[i - 1]
            if dt < 1e-6:
                continue
            ax = (velocities[i][0] - velocities[i - 1][0]) / dt
            ay = (velocities[i][1] - velocities[i - 1][1]) / dt
            accel_mag = np.sqrt(ax**2 + ay**2)
            accelerations.append(accel_mag)

        if not accelerations:
            return 0.0

        # Jerk score = normalized variance of acceleration
        accel_var = float(np.var(accelerations))
        # Normalize to 0-1 using sigmoid-like mapping
        score = min(1.0, accel_var / _MAX_JERK_VARIANCE)
        return score

    @staticmethod
    def _compute_click_burst_score(clicks: list[MouseClickEvent]) -> float:
        """
        Compute click burst score (0-1).

        Measures rapid repeated clicking. Higher score means more
        rapid clicking bursts.
        """
        if len(clicks) < 2:
            return 0.0

        # Only count press events
        presses = [c for c in clicks if c.pressed]
        if len(presses) < 2:
            return 0.0

        # Count pairs of clicks within burst interval
        burst_count = 0
        for i in range(1, len(presses)):
            dt = presses[i].timestamp - presses[i - 1].timestamp
            if dt < _CLICK_BURST_INTERVAL_S:
                burst_count += 1

        # Score: fraction of click pairs that are bursts
        total_pairs = len(presses) - 1
        return float(np.clip(burst_count / total_pairs, 0.0, 1.0))

    @staticmethod
    def _compute_click_frequency(
        clicks: list[MouseClickEvent], window_seconds: float,
    ) -> float:
        """Compute clicks per second."""
        if window_seconds < 1e-6:
            return 0.0

        presses = [c for c in clicks if c.pressed]
        return len(presses) / window_seconds

    @staticmethod
    def _compute_keyboard_burst_score(key_events: list[KeyEvent]) -> float:
        """
        Compute keyboard burst score (0-1).

        Measures typing intensity spikes — rapid sequences of keystrokes.
        """
        presses = [k for k in key_events if k.pressed and k.key_type == KeyType.REGULAR]
        if len(presses) < 2:
            return 0.0

        # Count pairs within burst interval
        burst_count = 0
        for i in range(1, len(presses)):
            dt = presses[i].timestamp - presses[i - 1].timestamp
            if dt < _KEY_BURST_INTERVAL_S:
                burst_count += 1

        total_pairs = len(presses) - 1
        return float(np.clip(burst_count / total_pairs, 0.0, 1.0))

    @staticmethod
    def _compute_keystroke_interval_variance(key_events: list[KeyEvent]) -> float:
        """
        Compute variance of keystroke intervals (ms^2).

        Uses only regular key presses (not modifiers/navigation).
        """
        presses = [k for k in key_events if k.pressed and k.key_type == KeyType.REGULAR]
        if len(presses) < 2:
            return 0.0

        intervals_ms = []
        for i in range(1, len(presses)):
            dt_ms = (presses[i].timestamp - presses[i - 1].timestamp) * 1000.0
            intervals_ms.append(dt_ms)

        return float(np.var(intervals_ms))

    @staticmethod
    def _compute_backspace_density(key_events: list[KeyEvent]) -> float:
        """
        Compute backspace density (ratio of backspaces to total keystrokes).

        Returns:
            0-1, higher means more deletions relative to typing.
        """
        presses = [k for k in key_events if k.pressed]
        if not presses:
            return 0.0

        # Count only regular + backspace (exclude modifiers/navigation)
        typing_keys = [
            k for k in presses
            if k.key_type in (KeyType.REGULAR, KeyType.BACKSPACE)
        ]
        if not typing_keys:
            return 0.0

        backspaces = sum(1 for k in typing_keys if k.key_type == KeyType.BACKSPACE)
        return float(np.clip(backspaces / len(typing_keys), 0.0, 1.0))

    @staticmethod
    def _compute_correction_rate_per_100_keys(key_events: list[KeyEvent]) -> float:
        """Compute correction events per 100 typed keys."""
        presses = [k for k in key_events if k.pressed]
        if not presses:
            return 0.0
        typing_keys = [k for k in presses if k.key_type in (KeyType.REGULAR, KeyType.BACKSPACE)]
        if not typing_keys:
            return 0.0
        corrections = sum(1 for k in typing_keys if k.key_type == KeyType.BACKSPACE)
        return float((corrections / len(typing_keys)) * 100.0)

    @staticmethod
    def _compute_inactivity(
        moves: list[MouseMoveEvent],
        clicks: list[MouseClickEvent],
        scrolls: list[MouseScrollEvent],
        keys: list[KeyEvent],
        current_time: float,
    ) -> float:
        """
        Compute seconds since last input event.

        Returns:
            Seconds of inactivity.
        """
        latest = 0.0

        if moves:
            latest = max(latest, moves[-1].timestamp)
        if clicks:
            latest = max(latest, clicks[-1].timestamp)
        if scrolls:
            latest = max(latest, scrolls[-1].timestamp)
        if keys:
            latest = max(latest, keys[-1].timestamp)

        if latest == 0.0:
            return current_time  # No events at all — full window is inactive

        return max(0.0, current_time - latest)

    @staticmethod
    def _compute_window_switch_rate(
        window_events: list[WindowFocusEvent],
        window_seconds: float,
    ) -> float:
        """
        Compute window/app switches per minute.

        Returns:
            Switches per minute.
        """
        if not window_events or window_seconds < 1e-6:
            return 0.0

        # Each event is a switch (except possibly the first which may be initial state)
        n_switches = max(0, len(window_events) - 1)
        minutes = window_seconds / 60.0
        return n_switches / minutes if minutes > 0 else 0.0

    @staticmethod
    def _compute_scroll_reversal_score(scrolls: list[MouseScrollEvent]) -> float:
        """
        Compute scroll direction reversal score (0-1).

        Frequent direction changes indicate confusion or searching behavior.
        """
        if len(scrolls) < 2:
            return 0.0

        reversals = 0
        for i in range(1, len(scrolls)):
            if scrolls[i].direction != scrolls[i - 1].direction:
                reversals += 1

        total_pairs = len(scrolls) - 1
        return float(np.clip(reversals / total_pairs, 0.0, 1.0))

    @staticmethod
    def _compute_scroll_back_rate_per_min(
        scrolls: list[MouseScrollEvent],
        window_seconds: float,
    ) -> float:
        """Compute upward scroll-back rate per minute."""
        if window_seconds <= 1e-6 or not scrolls:
            return 0.0
        upward = sum(1 for s in scrolls if s.direction == ScrollDirection.UP)
        return float((upward * 60.0) / window_seconds)
