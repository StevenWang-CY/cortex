"""Tests for stress integral tracker (biological pomodoros)."""
import pytest
from cortex.services.state_engine.stress_integral import StressIntegralTracker


class TestStressIntegralTracker:
    def test_no_stress_when_hrv_above_baseline(self):
        """HRV above baseline should not accumulate stress."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=55.0, timestamp=0.0)
        tracker.update(hrv_rmssd=60.0, timestamp=1.0)
        assert tracker.current_load == 0.0

    def test_stress_accumulates_when_hrv_drops(self):
        """HRV below baseline should accumulate stress load."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # 20ms deficit * 1s
        assert tracker.current_load > 0.0

    def test_stress_accumulation_trapezoidal(self):
        """Verify trapezoidal integration: (0 + 20)/2 * 1s = 10."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)  # suppression = 0
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)  # suppression = 20
        # Trapezoidal: avg(0, 20) * 1.0 = 10.0
        assert abs(tracker.current_load - 10.0) < 0.001

    def test_should_break_at_threshold(self):
        """Should recommend break when integral crosses threshold."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        # Drop HRV to 0 for extended period -> should hit threshold
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        for i in range(1, 10):
            tracker.update(hrv_rmssd=0.0, timestamp=float(i))
        assert tracker.should_break()

    def test_should_break_fires_only_once(self):
        """should_break() returns True only once per threshold crossing."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        for i in range(1, 20):
            tracker.update(hrv_rmssd=0.0, timestamp=float(i))
        assert tracker.should_break()  # first call: True
        assert not tracker.should_break()  # second call: False

    def test_not_break_below_threshold(self):
        """Should not break when integral is below threshold."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)
        assert not tracker.should_break()

    def test_reset_clears_integral(self):
        """reset() should zero the integral and clear break flag."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=10.0, timestamp=1.0)
        assert tracker.current_load > 0.0
        tracker.reset()
        assert tracker.current_load == 0.0
        assert not tracker.should_break()

    def test_sensitivity_multiplier_scales_threshold(self):
        """Higher sensitivity multiplier should make threshold harder to reach."""
        tracker = StressIntegralTracker(
            hrv_baseline=50.0, threshold=100.0, sensitivity_multiplier=2.0,
        )
        assert tracker.threshold == 200.0

    def test_update_sensitivity(self):
        """update_sensitivity() changes the effective threshold."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        assert tracker.threshold == 100.0
        tracker.update_sensitivity(1.5)
        assert tracker.threshold == 150.0

    def test_update_sensitivity_clamped(self):
        """Sensitivity multiplier is clamped to [0.5, 2.0]."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        tracker.update_sensitivity(5.0)
        assert tracker.threshold == 200.0  # clamped to 2.0
        tracker.update_sensitivity(0.1)
        assert tracker.threshold == 50.0  # clamped to 0.5

    def test_load_ratio(self):
        """load_ratio should be integral / effective threshold."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=0.0, timestamp=1.0)  # avg(0, 50)*1 = 25
        expected_ratio = tracker.current_load / 100.0
        assert abs(tracker.load_ratio - expected_ratio) < 0.001

    def test_none_hrv_skipped(self):
        """None HRV value should be skipped without affecting integral."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=None, timestamp=1.0)
        assert tracker.current_load == 0.0

    def test_large_gap_ignored(self):
        """Gaps > 30s between updates should be ignored."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=0.0, timestamp=60.0)  # 60s gap > 30s limit
        assert tracker.current_load == 0.0

    def test_serialization(self):
        """to_dict/from_dict should preserve state."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=100.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)

        data = tracker.to_dict()
        assert "integral" in data
        assert "base_threshold" in data
        assert "sensitivity_multiplier" in data

        tracker2 = StressIntegralTracker.from_dict(data)
        assert abs(tracker2.current_load - tracker.current_load) < 0.001

    def test_serialization_preserves_break_state(self):
        """Serialization should preserve break_emitted flag."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=10.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=0.0, timestamp=1.0)
        tracker.should_break()  # Mark as emitted

        data = tracker.to_dict()
        assert data["break_emitted"] is True

        tracker2 = StressIntegralTracker.from_dict(data)
        assert not tracker2.should_break()  # Already emitted

    def test_get_history(self):
        """get_history() should return recorded (timestamp, integral) pairs."""
        tracker = StressIntegralTracker(hrv_baseline=50.0, threshold=500.0)
        tracker.update(hrv_rmssd=50.0, timestamp=0.0)
        tracker.update(hrv_rmssd=30.0, timestamp=1.0)
        tracker.update(hrv_rmssd=20.0, timestamp=2.0)

        history = tracker.get_history()
        assert len(history) == 3
        # Timestamps should be ascending
        assert history[0][0] < history[1][0] < history[2][0]
