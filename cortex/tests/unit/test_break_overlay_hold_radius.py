"""A breath hold keeps the circle where the previous phase left it.

``expected_radius_ratio`` drew every "Hold" at the full 0.62 radius. Box
breathing -- the default guided pattern -- is Inhale, Hold, Exhale, Hold, so
on every cycle the circle shrank through the exhale, then jumped back to full
size for the empty-lungs hold, then snapped small again at the next inhale:
the picture told the user to hold a full breath when the cue said to hold
empty.
"""

from __future__ import annotations

import pytest

from cortex.apps.desktop_shell.break_overlay import _PATTERN_CYCLES, BreakOverlayWindow

FULL = 0.62
EMPTY = 0.32


def test_box_hold_after_inhale_is_full() -> None:
    phase, ratio = BreakOverlayWindow.expected_radius_ratio(_PATTERN_CYCLES["box"], 6.0)
    assert phase == "Hold"
    assert ratio == pytest.approx(FULL)


def test_box_hold_after_exhale_is_empty() -> None:
    phase, ratio = BreakOverlayWindow.expected_radius_ratio(_PATTERN_CYCLES["box"], 13.0)
    assert phase == "Hold"
    assert ratio == pytest.approx(EMPTY)


def test_box_cycle_has_no_jumps() -> None:
    """Sampled every 50 ms across two cycles, the radius never jumps."""
    cycle = _PATTERN_CYCLES["box"]
    previous = BreakOverlayWindow.expected_radius_ratio(cycle, 0.0)[1]
    for step in range(1, 32 * 20):
        ratio = BreakOverlayWindow.expected_radius_ratio(cycle, step * 0.05)[1]
        assert abs(ratio - previous) < 0.01, f"jump at t={step * 0.05:.2f}s"
        previous = ratio


def test_four_seven_eight_hold_is_full() -> None:
    phase, ratio = BreakOverlayWindow.expected_radius_ratio(_PATTERN_CYCLES["4-7-8"], 6.0)
    assert phase == "Hold"
    assert ratio == pytest.approx(FULL)
