"""Tests for StressIntegralTracker — biological Pomodoro break detection."""
from cortex.services.state_engine.stress_integral import StressIntegralTracker


class TestStressIntegralTracker:
    # Tests pin hrv_sigma=1.0 so the integral is in raw ms*s, matching the
    # literal suppression values used in the assertions below. The production
    # default (sigma=10ms) makes the integral a z-score deficit, which is
    # covered separately in test_sigma_normalization.
    def test_no_accumulation_above_baseline(self):
        """HRV above baseline → no stress accumulation."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0)
        tracker.update(hrv_rmssd=60.0, timestamp=0.0)
        tracker.update(hrv_rmssd=55.0, timestamp=1.0)
        assert tracker.current_load == 0.0

    def test_accumulates_below_baseline(self):
        """HRV below baseline → stress accumulates."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # 20ms suppression
        assert tracker.current_load > 0.0

    def test_trapezoidal_integration(self):
        """Verify trapezoidal integration for known values (raw ms*s units)."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0)
        tracker.update(hrv_rmssd=40.0, timestamp=0.0)  # suppression=10
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # suppression=20
        # Trapezoid: (10 + 20) / 2 * 1.0 = 15.0
        assert abs(tracker.current_load - 15.0) < 0.1

    def test_sigma_normalization(self):
        """With sigma=10, the same trajectory produces z-scored integral 1.5."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=10.0)
        tracker.update(hrv_rmssd=40.0, timestamp=0.0)  # z=1
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # z=2
        assert abs(tracker.current_load - 1.5) < 0.01

    def test_should_break_at_threshold(self):
        """Sustained HRV drop → should_break() fires once."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=100.0)
        for i in range(200):
            tracker.update(hrv_rmssd=30.0, timestamp=float(i))
        assert tracker.should_break() is True
        # Should not fire again
        assert tracker.should_break() is False

    def test_reset_clears_integral(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=50.0)
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
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=60.0)  # 60s gap
        assert tracker.current_load == 0.0

    def test_load_ratio(self):
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=100.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=2.0)  # suppression=20*2=40, ratio=0.4
        ratio = tracker.load_ratio
        assert 0.0 < ratio < 1.0

    def test_serialization_roundtrip(self):
        tracker = StressIntegralTracker(hrv_baseline=45.0, threshold=200.0)
        tracker.update(hrv_rmssd=30.0, timestamp=0.0)
        tracker.update(hrv_rmssd=25.0, timestamp=2.0)
        data = tracker.to_dict()
        restored = StressIntegralTracker.from_dict(data)
        assert abs(restored.current_load - tracker.current_load) < 0.01
