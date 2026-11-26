"""Cumulative physiological strain, the basis of body-aware break suggestions.

A fixed timer suggests a break after N minutes whatever the last N minutes
were like.  This tracker integrates the physiology block's strain reading
instead: time spent with heart rate up, heart-rate variability down and
breathing fast (all against the person's own reference) fills the load, calm
time drains it.

    load(t + dt) = load(t) * 0.5 ** ((1 - a) * dt / half_life) + a * dt

where ``a`` is the strain reading with its lowest part ignored (ordinary
fluctuation is not strain).  The unit is strain-seconds: ten minutes at full
strain, or roughly twenty-five at a steady moderate one, reach the default
threshold; brief spikes do not, and a calm stretch pays the load back.

The load is a pacing aid.  It is not a measure of stress, fatigue or health,
and the suggestion it produces is always the user's to ignore.
"""

from __future__ import annotations

import logging
from math import isfinite
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_SECONDS = 600.0
DEFAULT_RECOVERY_HALF_LIFE_SECONDS = 600.0
# Strain readings at or below this are treated as rest.
STRAIN_FLOOR = 0.15
# The physiology block must be at least this well observed for its reading to
# move the load; below it the load simply holds.
MIN_PHYSIOLOGY_COVERAGE = 0.30
# A gap longer than this is not observed time and earns neither load nor recovery.
MAX_INTEGRATION_GAP_SECONDS = 30.0
WARNING_RATIO = 0.80


def strain_reading(score: float, coverage: float) -> float | None:
    """Normalised strain in [0, 1], or ``None`` when physiology is too sparse.

    ``score`` and ``coverage`` are the physiology block's support-likely
    score and coverage.  Dividing one by the other asks "of the physiology
    that was observed, how much pointed to strain" so that a missing metric
    does not read as calm.
    """

    if not isfinite(score) or not isfinite(coverage) or coverage < MIN_PHYSIOLOGY_COVERAGE:
        return None
    return max(0.0, min(1.0, score / coverage))


class StrainLoadTracker:
    """Integrate strain readings into a load with recovery."""

    def __init__(
        self,
        *,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        recovery_half_life_seconds: float = DEFAULT_RECOVERY_HALF_LIFE_SECONDS,
        sensitivity: float = 1.0,
    ) -> None:
        if threshold_seconds <= 0 or recovery_half_life_seconds <= 0:
            raise ValueError("strain thresholds must be positive")
        self._base_threshold = float(threshold_seconds)
        self._half_life = float(recovery_half_life_seconds)
        self._sensitivity = 1.0
        self.update_sensitivity(sensitivity)
        self._load = 0.0
        self._last_timestamp: float | None = None
        self._warning_emitted = False
        self._break_emitted = False
        self._peak_load = 0.0

    @property
    def current_load(self) -> float:
        return self._load

    @property
    def peak_load(self) -> float:
        return self._peak_load

    @property
    def threshold(self) -> float:
        """Load at which a break is suggested (lower when more sensitive)."""

        return self._base_threshold / self._sensitivity

    @property
    def load_ratio(self) -> float:
        return self._load / self.threshold

    def update_sensitivity(self, multiplier: float) -> None:
        """Scale how readily a break is suggested (0.5 = half as readily)."""

        if isfinite(multiplier):
            self._sensitivity = max(0.5, min(2.0, float(multiplier)))

    def update(self, reading: float | None, timestamp: float) -> float:
        """Advance the load to ``timestamp`` with the strain seen since the last call.

        ``reading`` is :func:`strain_reading`'s output; ``None`` holds the
        load (physiology was not observed, which is neither strain nor rest).
        """

        if not isfinite(timestamp):
            return self._load
        previous = self._last_timestamp
        if previous is not None and timestamp <= previous:
            return self._load
        self._last_timestamp = timestamp
        if reading is None or previous is None:
            return self._load
        elapsed = timestamp - previous
        if elapsed > MAX_INTEGRATION_GAP_SECONDS:
            return self._load
        active = max(0.0, (min(1.0, reading) - STRAIN_FLOOR) / (1.0 - STRAIN_FLOOR))
        self._load = (
            self._load * 0.5 ** ((1.0 - active) * elapsed / self._half_life)
            + active * elapsed
        )
        self._peak_load = max(self._peak_load, self._load)
        # Re-arm once the load has clearly come back down, so a second
        # demanding stretch in the same session can be flagged again.
        if self.load_ratio < 0.5:
            self._warning_emitted = False
            self._break_emitted = False
        return self._load

    def should_warn(self) -> bool:
        """True once as the load first passes 80% of the threshold."""

        if self._warning_emitted or self.load_ratio < WARNING_RATIO:
            return False
        self._warning_emitted = True
        return True

    def should_break(self) -> bool:
        """True once per crossing of the threshold."""

        if self._break_emitted or self._load < self.threshold:
            return False
        self._break_emitted = True
        self._warning_emitted = True
        logger.info(
            "Strain load %.0f reached threshold %.0f", self._load, self.threshold
        )
        return True

    def defer(self) -> None:
        """Give back a break suggestion that could not be shown.

        The interruption gate can refuse a suggestion (quiet mode, cooldown,
        a call in progress).  The crossing is then still owed.
        """

        self._break_emitted = False

    def apply_recovery_credit(self, seconds: float) -> None:
        """Pay back load after a confirmed restorative action (a taken break)."""

        if isfinite(seconds) and seconds > 0:
            self._load = max(0.0, self._load - float(seconds))

    def reset(self) -> None:
        self._load = 0.0
        self._last_timestamp = None
        self._warning_emitted = False
        self._break_emitted = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "load": self._load,
            "threshold": self.threshold,
            "load_ratio": self.load_ratio,
            "peak_load": self._peak_load,
            "sensitivity": self._sensitivity,
        }
