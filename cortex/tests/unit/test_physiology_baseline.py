"""Personal physiology reference: warm-up, spread floors, slow clipped adaptation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cortex.libs.schemas.state import UserBaselines
from cortex.services.state_engine.physiology_baseline import (
    _ABSOLUTE_SPREAD_FLOOR,
    _RELATIVE_SPREAD_FLOOR,
    MetricReference,
    PhysiologyBaselineTracker,
    PhysiologyMetricName,
    PhysiologyReference,
)

HR = PhysiologyMetricName.HEART_RATE


def _warmed(
    value: float = 70.0,
    *,
    metric: PhysiologyMetricName = HR,
) -> tuple[PhysiologyBaselineTracker, float]:
    """A tracker warmed on a perfectly steady signal; returns it and the last timestamp."""

    tracker = PhysiologyBaselineTracker()
    now = 0.0
    for index in range(60):  # 60 samples over 147.5 s: both warm-up conditions met
        now = index * 2.5
        tracker.observe(metric, value, now)
    assert tracker.reference().for_metric(metric).ready
    return tracker, now


def test_not_ready_until_both_sample_count_and_elapsed_time_are_met() -> None:
    """Warm-up needs 60 samples *and* 120 s; either alone is not enough."""

    many_fast = PhysiologyBaselineTracker()
    for index in range(200):  # 200 samples within 99.5 s
        many_fast.observe(HR, 70.0, index * 0.5)
    assert not many_fast.reference().heart_rate.ready

    few_slow = PhysiologyBaselineTracker()
    for index in range(59):  # 59 samples over 290 s
        few_slow.observe(HR, 70.0, index * 5.0)
    assert not few_slow.reference().heart_rate.ready
    few_slow.observe(HR, 70.0, 59 * 5.0)
    assert few_slow.reference().heart_rate.ready

    many_fast.observe(HR, 70.0, 120.0)
    assert many_fast.reference().heart_rate.ready


def test_ready_reference_is_centred_on_the_warm_up_median() -> None:
    tracker = PhysiologyBaselineTracker()
    values = [60.0 + (index % 21) for index in range(61)]  # 60..80, median 70
    values[5] = 140.0  # one outlier must not drag the centre
    for index, value in enumerate(values):
        tracker.observe(HR, value, index * 2.0)
    reference = tracker.reference().heart_rate
    assert reference.ready
    assert reference.source == "session"
    assert reference.center == pytest.approx(70.0, abs=1.0)
    # Other metrics saw nothing and stay unavailable.
    assert not tracker.reference().sdnn.ready
    assert tracker.reference().sdnn.source == "unavailable"


@pytest.mark.parametrize("metric", list(PhysiologyMetricName))
@pytest.mark.parametrize("scale", [0.6, 2.5], ids=["absolute-floor", "relative-floor"])
def test_steady_warm_up_spread_is_floored(metric: PhysiologyMetricName, scale: float) -> None:
    """A zero-variance warm-up still yields a spread at both floors."""

    absolute = _ABSOLUTE_SPREAD_FLOOR[metric]
    relative = _RELATIVE_SPREAD_FLOOR[metric]
    # The two floors cross at absolute / relative; pick a plausible centre on
    # the side where the chosen floor dominates.
    center = scale * absolute / relative
    tracker, _ = _warmed(center, metric=metric)
    reference = tracker.reference().for_metric(metric)
    assert reference.spread == pytest.approx(max(absolute, relative * center))
    assert reference.spread > 0.0


def test_floor_keeps_ordinary_fluctuation_from_reading_as_a_large_deviation() -> None:
    tracker, _ = _warmed(70.0)
    reference = tracker.reference().heart_rate
    assert abs(reference.z(72.0)) < 1.0
    assert abs(reference.z(68.0)) < 1.0


def test_implausible_nan_and_non_increasing_samples_are_ignored() -> None:
    tracker = PhysiologyBaselineTracker(warmup_seconds=9.0, warmup_samples=10)
    now = 0.0
    for index in range(9):
        now = float(index)
        tracker.observe(HR, 70.0 + (index % 3), now)
        # Garbage interleaved with every clean sample.
        tracker.observe(HR, 500.0, now + 0.1)  # implausible
        tracker.observe(HR, 10.0, now + 0.2)  # implausible
        tracker.observe(HR, float("nan"), now + 0.3)
        tracker.observe(HR, float("inf"), now + 0.4)
        tracker.observe(HR, 71.0, float("nan"))
        tracker.observe(HR, 150.0, now)  # repeated timestamp
        tracker.observe(HR, 150.0, now - 0.5)  # backwards in time
        tracker.observe(HR, None, now + 0.5)
    # Nine clean samples: garbage did not count towards the warm-up.
    assert not tracker.reference().heart_rate.ready
    tracker.observe(HR, 71.0, 9.0)
    reference = tracker.reference().heart_rate
    assert reference.ready
    assert reference.center == pytest.approx(71.0)

    # Once ready, garbage still cannot move the reference.
    for value, stamp in ((500.0, 10.0), (float("nan"), 11.0), (150.0, 9.0), (150.0, 8.0)):
        tracker.observe(HR, value, stamp)
    assert tracker.reference().heart_rate == reference


def test_long_excursion_moves_the_centre_only_slowly() -> None:
    """Ten minutes far above the reference reads as a deviation, not a new normal."""

    tracker, now = _warmed(70.0)
    start = tracker.reference().heart_rate
    for second in range(1, 601):
        tracker.observe(HR, 160.0, now + second)
    after = tracker.reference().heart_rate
    assert after.center - start.center < 3.0
    # 160 BPM is still read as far outside the person's normal.
    assert after.z(160.0) > 5.0


def test_single_update_is_clipped_and_a_gap_earns_at_most_thirty_seconds() -> None:
    tracker, now = _warmed(70.0)
    start = tracker.reference().heart_rate
    # One extreme value after a ten-minute gap.
    tracker.observe(HR, 220.0, now + 600.0)
    after = tracker.reference().heart_rate
    weight_30s = 1.0 - 0.5 ** (30.0 / 3600.0)
    max_step = weight_30s * 3.0 * start.spread
    assert after.center - start.center == pytest.approx(max_step, rel=1e-6)


def test_centre_half_life_is_one_hour() -> None:
    """A sustained in-range shift closes half the distance in an hour of observation."""

    tracker, now = _warmed(70.0)
    start = tracker.reference().heart_rate
    target = start.center + start.spread  # inside the clip band
    for second in range(1, 3601):
        tracker.observe(HR, target, now + second)
    after = tracker.reference().heart_rate
    assert after.center == pytest.approx(start.center + 0.5 * (target - start.center), abs=0.05)


def _calibrated(distributions: dict[str, dict[str, float]]) -> UserBaselines:
    return UserBaselines(
        calibrated_at=datetime(2026, 11, 1, tzinfo=UTC),
        metric_distributions=distributions,
    )


def test_calibration_seeds_only_measured_metrics() -> None:
    tracker = PhysiologyBaselineTracker(
        _calibrated(
            {
                "hr": {"mu": 64.0, "sigma": 4.0},
                "hrv_sdnn_ms": {"mean": 42.0, "std": 12.0},
            }
        )
    )
    reference = tracker.reference()
    assert reference.heart_rate.ready and reference.heart_rate.source == "calibration"
    assert reference.heart_rate.center == 64.0
    assert reference.heart_rate.spread == pytest.approx(4.0)
    assert reference.sdnn.ready and reference.sdnn.center == 42.0
    assert reference.sdnn.spread == pytest.approx(12.0)
    # Not in the measured distributions: the population default is not adopted.
    assert not reference.respiration.ready
    assert not reference.blink_rate.ready


def test_calibration_spread_is_floored_and_a_missing_sigma_falls_back_to_the_floor() -> None:
    tracker = PhysiologyBaselineTracker(
        _calibrated({"heart_rate_bpm": {"mu": 80.0, "sigma": 0.1}, "respiration_rate": {"mu": 15.0}})
    )
    reference = tracker.reference()
    assert reference.heart_rate.spread == pytest.approx(
        max(_ABSOLUTE_SPREAD_FLOOR[HR], _RELATIVE_SPREAD_FLOOR[HR] * 80.0)
    )
    assert reference.respiration.ready
    assert reference.respiration.spread == pytest.approx(
        max(
            _ABSOLUTE_SPREAD_FLOOR[PhysiologyMetricName.RESPIRATION],
            _RELATIVE_SPREAD_FLOOR[PhysiologyMetricName.RESPIRATION] * 15.0,
        )
    )


def test_uncalibrated_profile_seeds_nothing_even_with_distributions() -> None:
    uncalibrated = UserBaselines(metric_distributions={"hr": {"mu": 64.0, "sigma": 4.0}})
    assert not uncalibrated.is_calibrated
    tracker = PhysiologyBaselineTracker(uncalibrated)
    assert tracker.reference() == PhysiologyReference()

    # A calibrated profile without measured distributions also seeds nothing:
    # hr_baseline/resp_baseline/blink_rate_baseline are population placeholders.
    placeholders_only = PhysiologyBaselineTracker(_calibrated({}))
    assert placeholders_only.reference() == PhysiologyReference()


def test_non_finite_calibration_centre_is_ignored() -> None:
    tracker = PhysiologyBaselineTracker(_calibrated({"hr": {"mu": float("nan"), "sigma": 4.0}}))
    assert not tracker.reference().heart_rate.ready


def test_session_warm_up_still_runs_for_metrics_calibration_did_not_seed() -> None:
    tracker = PhysiologyBaselineTracker(_calibrated({"hr": {"mu": 64.0, "sigma": 4.0}}))
    for index in range(60):
        tracker.observe(PhysiologyMetricName.BLINK_RATE, 18.0, index * 2.5)
    reference = tracker.reference()
    assert reference.blink_rate.ready and reference.blink_rate.source == "session"
    assert reference.heart_rate.source == "calibration"


def test_z_is_zero_while_not_ready() -> None:
    unready = MetricReference(center=70.0, spread=5.0, source="unavailable", ready=False)
    assert unready.z(150.0) == 0.0
    assert PhysiologyBaselineTracker().reference().heart_rate.z(200.0) == 0.0
    degenerate = MetricReference(center=70.0, spread=0.0, source="session", ready=True)
    assert degenerate.z(150.0) == 0.0
    ready = MetricReference(center=70.0, spread=5.0, source="session", ready=True)
    assert ready.z(80.0) == pytest.approx(2.0)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        PhysiologyBaselineTracker(warmup_seconds=-1.0)
    with pytest.raises(ValueError):
        PhysiologyBaselineTracker(warmup_samples=0)
    with pytest.raises(ValueError):
        PhysiologyBaselineTracker(half_life_seconds=0.0)


def test_calibration_seeds_through_the_keys_the_profile_actually_writes() -> None:
    """``CalibrationProfile.to_user_baselines`` names the levels ``hr``,
    ``resp_rate`` and ``blink_rate``; a tracker that only knew the long names
    silently ignored a measured breathing level."""

    baselines = UserBaselines(
        calibrated_at=datetime(2026, 11, 25, tzinfo=UTC),
        metric_distributions={
            "hr": {"mu": 64.0, "sigma": 4.0},
            "resp_rate": {"mu": 13.0, "sigma": 2.0},
            "blink_rate": {"mu": 16.0, "sigma": 5.0},
        },
    )
    assert baselines.is_calibrated
    reference = PhysiologyBaselineTracker(baselines).reference()
    assert reference.heart_rate.ready and reference.heart_rate.center == 64.0
    assert reference.respiration.ready and reference.respiration.center == 13.0
    assert reference.blink_rate.ready and reference.blink_rate.center == 16.0
    assert reference.respiration.source == "calibration"
    assert not reference.sdnn.ready


def test_a_later_calibration_replaces_a_session_learned_level() -> None:
    """A calibration taken mid-session is adopted; unmeasured metrics keep
    what the session learned."""

    tracker = PhysiologyBaselineTracker(warmup_seconds=10.0, warmup_samples=5)
    for tick in range(8):
        tracker.observe(PhysiologyMetricName.HEART_RATE, 80.0, 2.5 * tick)
        tracker.observe(PhysiologyMetricName.SDNN, 40.0, 2.5 * tick)
    learned = tracker.reference()
    assert learned.heart_rate.source == "session"
    assert learned.sdnn.source == "session"

    tracker.seed_from_calibration(
        UserBaselines(
            calibrated_at=datetime(2026, 11, 25, tzinfo=UTC),
            metric_distributions={"hr": {"mu": 66.0, "sigma": 5.0}},
        )
    )
    adopted = tracker.reference()
    assert adopted.heart_rate.source == "calibration"
    assert adopted.heart_rate.center == 66.0
    assert adopted.sdnn == learned.sdnn
