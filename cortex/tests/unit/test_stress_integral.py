"""Tests for StressIntegralTracker — biological Pomodoro break detection."""
from cortex.services.state_engine.stress_integral import StressIntegralTracker


class TestStressIntegralTracker:
    # P1 Pipeline F: tracker now enforces a 5 ms sigma floor so the integral
    # accumulates in ~25 minutes instead of ~25 seconds on a typical RMSSD
    # baseline. Tests that need raw ms*s units pin ``hrv_sigma=5.0`` (the
    # minimum) and adjust expected suppression values accordingly. The
    # production default (sigma derived from per-user dispersion) still
    # produces z-score deficits, covered in test_sigma_normalization.
    def test_no_accumulation_above_baseline(self):
        """HRV above baseline → no stress accumulation."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0)
        tracker.update(hrv_rmssd=60.0, timestamp=0.0)
        tracker.update(hrv_rmssd=55.0, timestamp=1.0)
        assert tracker.current_load == 0.0

    def test_accumulates_below_baseline(self):
        """HRV below baseline → stress accumulates."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # 20ms suppression
        assert tracker.current_load > 0.0

    def test_trapezoidal_integration(self):
        """Verify trapezoidal integration for known values (sigma=5 ms floor)."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0)
        tracker.update(hrv_rmssd=40.0, timestamp=0.0)  # suppression=10/5=2.0
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # suppression=20/5=4.0
        # Trapezoid: (2.0 + 4.0) / 2 * 1.0 = 3.0
        assert abs(tracker.current_load - 3.0) < 0.01

    def test_sigma_normalization(self):
        """With sigma=10, the same trajectory produces z-scored integral 1.5."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=10.0)
        tracker.update(hrv_rmssd=40.0, timestamp=0.0)  # z=1
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # z=2
        assert abs(tracker.current_load - 1.5) < 0.01

    def test_sigma_floor_enforced(self):
        """Sigma < 5 ms is clamped to 5 ms (P1 Pipeline F)."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0)
        # The integral should match sigma=5 behaviour, not sigma=1.
        tracker.update(hrv_rmssd=40.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)
        # If the floor were 1 ms the value would be 15.0 (legacy bug); with
        # the 5 ms floor it is 3.0.
        assert abs(tracker.current_load - 3.0) < 0.01

    def test_should_break_at_threshold(self):
        """Sustained HRV drop → should_break() fires once."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0, threshold=100.0)
        # With sigma=5 and 20 ms suppression, the integrand is 4.0/s.
        # 25 s reaches load = 100. Take ~30 s to clear the threshold.
        for i in range(30):
            tracker.update(hrv_rmssd=30.0, timestamp=float(i))
        assert tracker.should_break() is True
        # Should not fire again
        assert tracker.should_break() is False

    def test_reset_clears_integral(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0, threshold=50.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=5.0)
        tracker.reset()
        assert tracker.current_load == 0.0
        assert tracker.should_break() is False

    def test_sensitivity_multiplier_adjusts_threshold(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        assert tracker.threshold == 100.0
        tracker.update_sensitivity(2.0)
        assert tracker.threshold == 200.0

    def test_none_hrv_skipped(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0)
        result = tracker.update(hrv_rmssd=None, timestamp=0.0)
        assert result == 0.0

    def test_large_gap_ignored(self):
        """Gaps > 30s should not accumulate."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=60.0)  # 60s gap
        assert tracker.current_load == 0.0

    def test_load_ratio(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=5.0, threshold=100.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        # suppression=20/5=4.0; dt=2s; integral=8.0; ratio=0.08
        tracker.update(hrv_rmssd=30.0, timestamp=2.0)
        ratio = tracker.load_ratio
        assert 0.0 < ratio < 1.0

    def test_serialization_roundtrip(self):
        tracker = StressIntegralTracker(hrv_baseline=45.0, threshold=200.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=25.0, timestamp=2.0)
        data = tracker.to_dict()
        restored = StressIntegralTracker.from_dict(data)
        assert abs(restored.current_load - tracker.current_load) < 0.01
