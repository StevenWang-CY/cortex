"""Conservative behavior-only reading check-in eligibility.

The legacy class/argument names are retained for compatibility. A quiet input
pattern does not establish whether somebody understands or absorbs a text.
Camera measurements never change the eligibility or timing of this reminder.
"""

from __future__ import annotations

import logging

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds

logger = logging.getLogger(__name__)

# Detection thresholds
_MOUSE_VELOCITY_MAX = 30.0   # px/s — minimal mouse movement
_MIN_DURATION_SECONDS = 120.0  # Conservative sustained behavior-only dwell
_COOLDOWN_SECONDS = 300.0  # 5 min cooldown between triggers

# Browser app names (macOS)
_BROWSER_APPS = frozenset({
    "Google Chrome", "Safari", "Firefox", "Arc",
    "Microsoft Edge", "Brave Browser", "Chromium",
    "chrome", "safari", "firefox", "arc", "edge", "brave",
})


class ZombieReadingDetector:
    """Offer a reading check-in after sustained quiet input in a browser.

    ``blink_baseline`` and ``blink_rate`` are accepted for older callers but
    intentionally have no effect. They remain diagnostic-only measurements.
    """

    def __init__(
        self,
        blink_baseline: float = 17.0,
        min_duration: float = _MIN_DURATION_SECONDS,
        cooldown: float = _COOLDOWN_SECONDS,
        *,
        clock: Clock | None = None,
    ) -> None:
        # Audit D16: elapsed time comes from the injected clock so replay and
        # tests that freeze/advance time observe the same accumulator.
        self._clock = clock or SYSTEM_CLOCK
        self._blink_baseline = blink_baseline
        self._min_duration = min_duration
        self._cooldown = cooldown

        self._zombie_start: float | None = None
        self._last_trigger: float = 0.0
        self._consecutive_frames: int = 0

    def update_baseline(self, blink_baseline: float) -> None:
        """Retain an older caller's diagnostic baseline without using it."""
        self._blink_baseline = blink_baseline

    def update(
        self,
        state: str,
        mouse_velocity: float | None,
        blink_rate: float | None,
        active_app: str,
        current_time: float | None = None,
        may_trigger: bool = True,
    ) -> bool:
        """
        Check if sustained quiet browser behavior permits a check-in.

        Args:
            state: Current support state using the legacy wire names.
            mouse_velocity: Mean mouse velocity in px/s; None means unavailable.
            blink_rate: Deprecated diagnostic argument; ignored.
            active_app: Name of the currently active application.
            current_time: Override timestamp. None = use the injected clock.
            may_trigger: When False the detector keeps accumulating, but never
                fires. The cooldown and the accumulator were previously
                committed the moment the threshold was crossed, before the
                caller consulted the interruption gate, so a detection dropped
                by quiet mode, pause or the hourly cap was discarded rather
                than deferred and the whole dwell had to be served again.

        Returns:
            True if observed behavior has persisted long enough to offer help.
        """
        if current_time is None:
            current_time = monotonic_seconds(self._clock)

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
            if duration >= self._min_duration:
                if not may_trigger:
                    # Hold the accumulator so the dwell already served counts
                    # once interruptions are allowed again.
                    return False
                self._last_trigger = current_time
                self._reset()
                logger.info(
                    "Reading check-in eligible after %.0fs (app=%s)",
                    duration, active_app,
                )
                return True
        else:
            self._reset()

        return False

    def _check_conditions(
        self,
        state: str,
        mouse_velocity: float | None,
        blink_rate: float | None,
        active_app: str,
    ) -> bool:
        """Check if the required behavioral observations are available."""
        # A behavioral hypothesis, not a statement about cognition.
        if state != "HYPO":
            return False

        # Condition 2: Browser is active
        if active_app not in _BROWSER_APPS:
            return False

        # Condition 3: Low mouse velocity (reading, not interacting)
        if mouse_velocity is None or mouse_velocity > _MOUSE_VELOCITY_MAX:
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
        return monotonic_seconds(self._clock) - self._zombie_start
