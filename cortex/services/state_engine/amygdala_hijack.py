"""
State Engine — Amygdala Hijack Detection

Detects emotional flooding after a Wrong Answer submission on LeetCode.

The Amygdala Hijack Index (AAI) fuses three bio-signals:

    AAI(t) = α · max(0, dHR/dt)          heart-rate spike (sympathetic surge)
           - β · ΔBlinks(t)              blink suppression (hyper-focus / freeze)
           + γ · Velocity_keys(t)         frantic typing (fight response)

A hijack is confirmed when AAI exceeds a threshold AND the spike occurs
within a configurable time window of a Wrong Answer submission event.

When detected, the system should pause the user before they submit
another impulsive attempt.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import NamedTuple

logger = logging.getLogger(__name__)

_DEFAULT_ALPHA = 0.4
_DEFAULT_BETA = 0.3
_DEFAULT_GAMMA = 0.3
_DEFAULT_THRESHOLD = 0.7
_DEFAULT_WA_WINDOW_S = 5.0
_HISTORY_WINDOW_S = 60.0


class _AAISample(NamedTuple):
    timestamp: float
    score: float


class AmygdalaHijackDetector:
    """
    Detects amygdala hijack — the emotional flooding that follows a Wrong
    Answer submission, characterised by a heart-rate spike, blink suppression,
    and frantic key velocity.

    Usage::

        detector = AmygdalaHijackDetector()
        aai = detector.update(hr_delta=12.0, blink_delta=-3.0, key_velocity=0.8,
                              wa_timestamp=now - 2.0)
        if detector.is_hijacked():
            pause_user()
    """

    def __init__(
        self,
        alpha: float = _DEFAULT_ALPHA,
        beta: float = _DEFAULT_BETA,
        gamma: float = _DEFAULT_GAMMA,
        threshold: float = _DEFAULT_THRESHOLD,
        wa_window_s: float = _DEFAULT_WA_WINDOW_S,
    ) -> None:
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._threshold = threshold
        self._wa_window_s = wa_window_s

        self._history: deque[_AAISample] = deque()
        self._latest_aai: float = 0.0
        self._latest_hijacked: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        hr_delta: float,
        blink_delta: float,
        key_velocity: float,
        wa_timestamp: float | None = None,
        current_time: float | None = None,
    ) -> float:
        """
        Compute the Amygdala Hijack Index and update internal state.

        Args:
            hr_delta: Instantaneous heart-rate derivative (dHR/dt).
                      Positive = rising.
            blink_delta: Change in blink rate relative to baseline.
                         Negative = blink suppression.
            key_velocity: Normalised keystroke velocity [0, 1].
            wa_timestamp: Monotonic timestamp of the most recent Wrong Answer
                          submission.  ``None`` if no recent WA.
            current_time: Override for the current monotonic clock.  Defaults
                          to ``time.monotonic()``.

        Returns:
            The computed AAI score.
        """
        if current_time is None:
            current_time = time.monotonic()

        # AAI(t) = α·max(0, dHR/dt) - β·ΔBlinks(t) + γ·Velocity_keys(t)
        aai = (
            self._alpha * max(0.0, hr_delta)
            - self._beta * blink_delta
            + self._gamma * key_velocity
        )
        self._latest_aai = aai

        # Store in history and prune samples older than 60s
        self._history.append(_AAISample(timestamp=current_time, score=aai))
        cutoff = current_time - _HISTORY_WINDOW_S
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

        # Hijack = AAI above threshold AND within WA window
        above_threshold = aai > self._threshold
        within_wa_window = (
            wa_timestamp is not None
            and (current_time - wa_timestamp) <= self._wa_window_s
        )
        self._latest_hijacked = above_threshold and within_wa_window

        if self._latest_hijacked:
            logger.info(
                "Amygdala hijack detected: AAI=%.3f (threshold=%.2f), "
                "%.1fs after WA",
                aai,
                self._threshold,
                current_time - (wa_timestamp or current_time),
            )

        return aai

    def is_hijacked(self) -> bool:
        """Return True if the latest update indicated an amygdala hijack."""
        return self._latest_hijacked

    def reset(self) -> None:
        """Clear all accumulated state."""
        self._history.clear()
        self._latest_aai = 0.0
        self._latest_hijacked = False
