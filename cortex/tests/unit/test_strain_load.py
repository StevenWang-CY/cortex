"""Cumulative physiological strain load: integration, recovery, one-shot flags."""

from __future__ import annotations

import pytest

from cortex.services.state_engine.strain_load import (
    DEFAULT_RECOVERY_HALF_LIFE_SECONDS,
    DEFAULT_THRESHOLD_SECONDS,
    MAX_INTEGRATION_GAP_SECONDS,
    MIN_PHYSIOLOGY_COVERAGE,
    STRAIN_FLOOR,
    WARNING_RATIO,
    StrainLoadTracker,
    strain_reading,
)


def _run(
    tracker: StrainLoadTracker,
    reading: float | None,
    *,
    start: float,
    seconds: int,
    step: float = 1.0,
) -> float:
    """Feed ``reading`` every ``step`` seconds after ``start``; return the last timestamp."""

    now = start
    for _ in range(int(seconds / step)):
        now += step
        tracker.update(reading, now)
    return now


def _primed(reading: float | None = 1.0) -> tuple[StrainLoadTracker, float]:
    tracker = StrainLoadTracker()
    tracker.update(reading, 0.0)
    return tracker, 0.0


def test_strain_reading_abstains_below_the_coverage_floor() -> None:
    assert strain_reading(0.2, MIN_PHYSIOLOGY_COVERAGE - 1e-6) is None
    assert strain_reading(0.0, 0.0) is None
    assert strain_reading(float("nan"), 0.8) is None
    assert strain_reading(0.4, float("inf")) is None
    assert strain_reading(0.15, MIN_PHYSIOLOGY_COVERAGE) == pytest.approx(0.5)
    assert strain_reading(0.4, 0.8) == pytest.approx(0.5)
    # A missing metric must not read as calm: the ratio, not the raw score.
    assert strain_reading(0.35, 0.35) == pytest.approx(1.0)
    assert strain_reading(1.2, 0.9) == 1.0
    assert strain_reading(-0.1, 0.9) == 0.0


def test_first_reading_only_anchors_time() -> None:
    tracker = StrainLoadTracker()
    assert tracker.update(1.0, 100.0) == 0.0
    assert tracker.update(1.0, 101.0) == pytest.approx(1.0)


def test_ten_minutes_at_full_strain_reaches_the_default_threshold_once() -> None:
    tracker, now = _primed()
    assert tracker.threshold == DEFAULT_THRESHOLD_SECONDS
    breaks: list[float] = []
    warnings: list[float] = []
    for _ in range(900):
        now += 1.0
        tracker.update(1.0, now)
        if tracker.should_warn():
            warnings.append(now)
        if tracker.should_break():
            breaks.append(now)
    assert breaks == pytest.approx([DEFAULT_THRESHOLD_SECONDS])
    assert warnings == pytest.approx([WARNING_RATIO * DEFAULT_THRESHOLD_SECONDS])
    assert tracker.peak_load == pytest.approx(900.0)


def test_brief_spike_does_not_reach_the_threshold() -> None:
    tracker, now = _primed()
    _run(tracker, 1.0, start=now, seconds=120)
    assert tracker.current_load == pytest.approx(120.0)
    assert not tracker.should_break()
    assert not tracker.should_warn()


@pytest.mark.parametrize("calm", [0.0, STRAIN_FLOOR, 0.5 * STRAIN_FLOOR])
def test_calm_time_decays_the_load_with_the_documented_half_life(calm: float) -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=300)
    assert tracker.current_load == pytest.approx(300.0)
    now = _run(tracker, calm, start=now, seconds=int(DEFAULT_RECOVERY_HALF_LIFE_SECONDS))
    assert tracker.current_load == pytest.approx(150.0, rel=1e-9)
    _run(tracker, calm, start=now, seconds=int(DEFAULT_RECOVERY_HALF_LIFE_SECONDS))
    assert tracker.current_load == pytest.approx(75.0, rel=1e-9)


def test_moderate_strain_fills_more_slowly_than_full_strain() -> None:
    full, now = _primed()
    _run(full, 1.0, start=now, seconds=300)
    moderate, now = _primed(0.5)
    _run(moderate, 0.5, start=now, seconds=300)
    assert 0.0 < moderate.current_load < full.current_load


def test_unobserved_physiology_and_long_gaps_hold_the_load() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=100)
    held = tracker.current_load
    now = _run(tracker, None, start=now, seconds=300)
    assert tracker.current_load == held
    # A gap longer than the integration bound earns neither load nor recovery.
    now += MAX_INTEGRATION_GAP_SECONDS + 1.0
    tracker.update(1.0, now)
    assert tracker.current_load == held
    now += MAX_INTEGRATION_GAP_SECONDS + 60.0
    tracker.update(0.0, now)
    assert tracker.current_load == held
    # The clock is re-anchored, so ordinary integration resumes.
    tracker.update(1.0, now + 1.0)
    assert tracker.current_load == pytest.approx(held + 1.0)


def test_warning_fires_once_at_eighty_percent() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=479)
    assert not tracker.should_warn()
    now = _run(tracker, 1.0, start=now, seconds=1)
    assert tracker.load_ratio == pytest.approx(WARNING_RATIO)
    assert tracker.should_warn()
    _run(tracker, 1.0, start=now, seconds=10)
    assert not tracker.should_warn()


def test_defer_re_arms_a_refused_break() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=600)
    assert tracker.should_break()
    tracker.defer()
    now = _run(tracker, 1.0, start=now, seconds=1)
    assert tracker.should_break(), "a crossing the gate refused is still owed"
    _run(tracker, 1.0, start=now, seconds=1)
    assert not tracker.should_break()


def test_flags_re_arm_only_after_the_load_falls_below_half_the_threshold() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=600)
    assert tracker.should_break()

    # Dip to ~60% and come back: the same demanding stretch, no second flag.
    now = _run(tracker, 0.0, start=now, seconds=450)
    assert 0.5 < tracker.load_ratio < 0.7
    now = _run(tracker, 1.0, start=now, seconds=300)
    assert tracker.current_load >= tracker.threshold
    assert not tracker.should_break()
    assert not tracker.should_warn()

    # A clear recovery below half re-arms both flags for a second stretch.
    while tracker.load_ratio >= 0.5:
        now += 1.0
        tracker.update(0.0, now)
    while tracker.load_ratio < WARNING_RATIO:
        now += 1.0
        tracker.update(1.0, now)
    assert tracker.should_warn()
    while tracker.current_load < tracker.threshold:
        now += 1.0
        tracker.update(1.0, now)
    assert tracker.should_break()


@pytest.mark.parametrize(
    ("sensitivity", "expected"),
    [(1.0, 600.0), (2.0, 300.0), (0.5, 1200.0), (10.0, 300.0), (0.01, 1200.0)],
)
def test_sensitivity_scales_the_threshold_within_its_clamp(
    sensitivity: float, expected: float
) -> None:
    tracker = StrainLoadTracker(sensitivity=sensitivity)
    assert tracker.threshold == pytest.approx(expected)


def test_non_finite_sensitivity_keeps_the_previous_value() -> None:
    tracker = StrainLoadTracker(sensitivity=2.0)
    tracker.update_sensitivity(float("nan"))
    tracker.update_sensitivity(float("inf"))
    assert tracker.threshold == pytest.approx(300.0)


def test_more_sensitive_tracker_breaks_sooner() -> None:
    sensitive = StrainLoadTracker(sensitivity=2.0)
    sensitive.update(1.0, 0.0)
    _run(sensitive, 1.0, start=0.0, seconds=300)
    assert sensitive.should_break()


def test_non_finite_and_non_increasing_timestamps_are_ignored() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=10)
    assert tracker.current_load == pytest.approx(10.0)
    for bad in (float("nan"), float("inf"), now, now - 5.0):
        assert tracker.update(1.0, bad) == pytest.approx(10.0)
    # Integration resumes from the last *valid* timestamp.
    tracker.update(1.0, now + 1.0)
    assert tracker.current_load == pytest.approx(11.0)


def test_recovery_credit_and_reset() -> None:
    tracker, now = _primed()
    now = _run(tracker, 1.0, start=now, seconds=200)
    tracker.apply_recovery_credit(50.0)
    assert tracker.current_load == pytest.approx(150.0)
    tracker.apply_recovery_credit(float("nan"))
    tracker.apply_recovery_credit(-10.0)
    assert tracker.current_load == pytest.approx(150.0)
    tracker.apply_recovery_credit(1_000.0)
    assert tracker.current_load == 0.0
    tracker.reset()
    # After a reset the next reading only re-anchors the clock.
    assert tracker.update(1.0, now + 1.0) == 0.0


def test_invalid_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError):
        StrainLoadTracker(threshold_seconds=0.0)
    with pytest.raises(ValueError):
        StrainLoadTracker(recovery_half_life_seconds=-1.0)
