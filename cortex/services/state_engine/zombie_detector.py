"""
State Engine — Zombie-Reading Interception

Detects when the user is passively reading without absorbing content:
- HYPO state active (low arousal)
- Browser is the active app
- Limited mouse XY movement (< 30 px/s)
- Blink rate above baseline (glazed eyes)

All four conditions must be sustained for a minimum duration (default 90s)
before triggering. When detected, triggers an Active Recall overlay that
scrapes the visible text and generates a fill-in-the-blank question to
break the trance.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Detection thresholds
_SCROLL_VELOCITY_MAX = 50.0  # px/s — steady reading pace
_MOUSE_VELOCITY_MAX = 30.0   # px/s — minimal mouse movement
_MIN_DURATION_SECONDS = 90.0  # Must sustain for 90s before triggering
_NO_BLINK_MIN_DURATION_SECONDS = 120.0  # Require stronger evidence without camera kinematics
_BLINK_ELEVATION_RATIO = 1.15  # Blink rate 15% above baseline
_COOLDOWN_SECONDS = 300.0  # 5 min cooldown between triggers

# Browser app names (macOS)
_BROWSER_APPS = frozenset({
    "Google Chrome", "Safari", "Firefox", "Arc",
    "Microsoft Edge", "Brave Browser", "Chromium",
    "chrome", "safari", "firefox", "arc", "edge", "brave",
})


class ZombieReadingDetector:
    """
    Detects passive reading (zombie-reading) patterns.

    Monitors the combination of HYPO state, browser focus, slow scrolling,
    low mouse movement, and elevated blink rate to identify when the user
    is staring at text without absorbing it.

    Usage:
        detector = ZombieReadingDetector(blink_baseline=17.0)
        if detector.update(state, telemetry, kinematics, active_app):
            trigger_active_recall()
    """

    def __init__(
        self,
        blink_baseline: float = 17.0,
        min_duration: float = _MIN_DURATION_SECONDS,
        cooldown: float = _COOLDOWN_SECONDS,
    ) -> None:
        self._blink_baseline = blink_baseline
        self._min_duration = min_duration
        self._cooldown = cooldown

        self._zombie_start: float | None = None
        self._last_trigger: float = 0.0
        self._consecutive_frames: int = 0

    def update_baseline(self, blink_baseline: float) -> None:
        """Update blink rate baseline."""
        self._blink_baseline = blink_baseline

    def update(
        self,
        state: str,
        mouse_velocity: float,
        blink_rate: float | None,
        active_app: str,
        current_time: float | None = None,
    ) -> bool:
        """
        Check if zombie-reading conditions are met.

        Args:
            state: Current cognitive state ("FLOW", "HYPO", "HYPER", "RECOVERY").
            mouse_velocity: Mean mouse velocity in px/s.
            blink_rate: Current blink rate in blinks/min. None = no data.
            active_app: Name of the currently active application.
            current_time: Override timestamp. None = use time.monotonic().

        Returns:
            True if zombie-reading has been sustained long enough to trigger.
        """
        if current_time is None:
            current_time = time.monotonic()

        # Check cooldown
        if current_time - self._last_trigger < self._cooldown:
            self._reset()
            return False

        # Check all conditions
        is_zombie = self._check_conditions(
            state=state,
            mouse_velocity=mouse_velocity,
            blink_rate=blink_rate,
            active_app=active_app,
        )

        if is_zombie:
            if self._zombie_start is None:
                self._zombie_start = current_time
                self._consecutive_frames = 1
            else:
                self._consecutive_frames += 1

            duration = current_time - self._zombie_start
            required_duration = self._min_duration
            if blink_rate is None:
                required_duration = max(required_duration, _NO_BLINK_MIN_DURATION_SECONDS)
            if duration >= required_duration:
                self._last_trigger = current_time
                self._reset()
                logger.info(
                    "Zombie-reading detected after %.0fs (app=%s, mouse=%.1f, blink=%.1f)",
                    duration, active_app, mouse_velocity, blink_rate or 0.0,
                )
                return True
        else:
            self._reset()

        return False

    def _check_conditions(
        self,
        state: str,
        mouse_velocity: float,
        blink_rate: float | None,
        active_app: str,
    ) -> bool:
        """Check if all zombie-reading conditions are currently met."""
        # Condition 1: HYPO state (low arousal)
        if state != "HYPO":
            return False

        # Condition 2: Browser is active
        if active_app not in _BROWSER_APPS:
            return False

        # Condition 3: Low mouse velocity (reading, not interacting)
        if mouse_velocity > _MOUSE_VELOCITY_MAX:
            return False

        # Condition 4: Blink rate above baseline (glazed eyes). If camera
        # kinematics are unavailable, rely on the other sustained telemetry
        # conditions rather than disabling the detector entirely.
        if blink_rate is None:
            return True
        if blink_rate < self._blink_baseline * _BLINK_ELEVATION_RATIO:
            return False

        return True

    def _reset(self) -> None:
        """Reset the zombie detection accumulator."""
        self._zombie_start = None
        self._consecutive_frames = 0

    @property
    def is_accumulating(self) -> bool:
        """Whether zombie conditions are currently being accumulated."""
        return self._zombie_start is not None

    @property
    def accumulation_seconds(self) -> float:
        """How long zombie conditions have been sustained."""
        if self._zombie_start is None:
            return 0.0
        return time.monotonic() - self._zombie_start
