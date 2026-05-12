"""Unit tests for SessionReportGenerator."""

import time

import pytest

from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.session_report.models import SessionReport


class TestSessionReportGenerator:
    """Tests for session report generation."""

    def test_basic_report_generation(self):
        """Generate a report with minimal data."""
        gen = SessionReportGenerator()
        gen.start()

        now = time.time()
        gen.record_state("FLOW", now)
        gen.record_state("HYPER", now + 60)
        gen.record_state("FLOW", now + 90)

        gen.record_hr(72.0)
        gen.record_hr(75.0)
        gen.record_hrv(45.0)
        gen.record_stress(250.0)
        gen.record_break(recommended=True)
        gen.record_activity("Lecture 3", "educational", 300.0)
        gen.record_distraction("reddit.com")

        # End at now+150: 60s FLOW + 30s HYPER + 60s FLOW (final segment)
        report = gen.finish(end_timestamp=now + 150)

        assert isinstance(report, SessionReport)
        assert report.time_in_flow_seconds == pytest.approx(120.0, abs=1.0)
        assert report.time_in_hyper_seconds == pytest.approx(30.0, abs=1.0)
        assert report.peak_stress_integral == 250.0
        assert report.breaks_taken == 1
        assert report.breaks_recommended == 1
        assert report.avg_hr_bpm == 73.5
        assert report.avg_hrv_rmssd == 45.0
        assert len(report.state_transitions) == 2
        assert len(report.top_activities) == 1
        assert report.top_distraction_domains == ["reddit.com"]

    def test_flow_percentage(self):
        """Flow percentage is calculated from wall-clock time."""
        gen = SessionReportGenerator()
        gen.start()

        now = time.time()
        gen.record_state("FLOW", now)
        gen.record_state("HYPER", now + 100)

        # End at now+200: 100s FLOW + 100s HYPER (final segment)
        report = gen.finish(end_timestamp=now + 200)
        assert report.time_in_flow_seconds == pytest.approx(100.0, abs=1.0)
        assert report.flow_percentage > 0

    def test_empty_report(self):
        """Report with no data should not crash."""
        gen = SessionReportGenerator()
        gen.start()
        report = gen.finish()

        assert report.duration_seconds >= 0
        assert report.time_in_flow_seconds == 0.0
        assert report.avg_hr_bpm is None
        assert report.avg_hrv_rmssd is None
        assert report.longest_flow_streak_seconds == 0.0

    def test_longest_flow_streak(self):
        """Tracks the longest continuous FLOW period."""
        gen = SessionReportGenerator()
        gen.start()

        now = time.time()
        gen.record_state("FLOW", now)
        gen.record_state("HYPER", now + 30)   # 30s flow
        gen.record_state("FLOW", now + 40)
        gen.record_state("HYPO", now + 120)   # 80s flow

        report = gen.finish(end_timestamp=now + 150)
        assert report.longest_flow_streak_seconds == pytest.approx(80.0, abs=1.0)

    def test_final_state_duration_included(self):
        """The last state segment must be included in duration totals (bug fix)."""
        gen = SessionReportGenerator()
        gen.start()

        now = time.time()
        gen.record_state("FLOW", now)
        # Session ends at now+120 with no further state transitions.
        # The final 120s of FLOW must be counted.
        report = gen.finish(end_timestamp=now + 120)

        assert report.time_in_flow_seconds == pytest.approx(120.0, abs=1.0)
        assert report.longest_flow_streak_seconds == pytest.approx(120.0, abs=1.0)

    def test_final_hyper_segment_included(self):
        """Final non-FLOW segment must also be counted."""
        gen = SessionReportGenerator()
        gen.start()

        now = time.time()
        gen.record_state("FLOW", now)
        gen.record_state("HYPER", now + 60)
        # Session ends at now+160: 60s FLOW then 100s HYPER (final segment)
        report = gen.finish(end_timestamp=now + 160)

        assert report.time_in_flow_seconds == pytest.approx(60.0, abs=1.0)
        assert report.time_in_hyper_seconds == pytest.approx(100.0, abs=1.0)


class TestStressIntegralWarning:
    """Tests for pre-break warning at 80% threshold."""

    def test_warning_at_80_percent(self):
        from cortex.services.state_engine.stress_integral import StressIntegralTracker

        # hrv_sigma=1.0 preserves raw-ms integration so 1ms*s/sample arithmetic holds.
        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=100.0)

        # Push to 80% (suppression=1ms/s, need 81 samples for 80ms integral)
        for i in range(82):
            tracker.update(hrv_rmssd=49.0, timestamp=float(i))

        assert tracker.should_warn() is True
        assert tracker.should_break() is False

    def test_no_warning_below_80(self):
        from cortex.services.state_engine.stress_integral import StressIntegralTracker

        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=1000.0)
        tracker.update(hrv_rmssd=49.0, timestamp=0.0)
        tracker.update(hrv_rmssd=49.0, timestamp=1.0)

        assert tracker.should_warn() is False

    def test_warning_fires_only_once(self):
        from cortex.services.state_engine.stress_integral import StressIntegralTracker

        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=100.0)
        for i in range(82):
            tracker.update(hrv_rmssd=49.0, timestamp=float(i))

        assert tracker.should_warn() is True
        assert tracker.should_warn() is False  # Second call returns False

    def test_reset_clears_warning(self):
        from cortex.services.state_engine.stress_integral import StressIntegralTracker

        tracker = StressIntegralTracker(hrv_baseline=50.0, hrv_sigma=1.0, threshold=100.0)
        for i in range(82):
            tracker.update(hrv_rmssd=49.0, timestamp=float(i))
        tracker.should_warn()
        tracker.reset()

        # After reset, warning should fire again when threshold approached
        for i in range(82):
            tracker.update(hrv_rmssd=49.0, timestamp=float(100 + i))
        assert tracker.should_warn() is True


class TestTopicCalibration:
    """Tests for subject-specific difficulty calibration."""

    def test_topic_difficulty_insufficient_data(self):
        from cortex.services.state_engine.longitudinal import LongitudinalTracker

        tracker = LongitudinalTracker()
        assert tracker.get_topic_difficulty("algorithms") is None

    def test_topic_difficulty_high_hyper(self):
        from cortex.services.state_engine.longitudinal import LongitudinalTracker

        tracker = LongitudinalTracker()
        tracker.set_topic("algorithms")

        # Simulate 100s of study: 70s HYPER, 30s FLOW
        for _i in range(140):
            tracker.accumulate(hr=80.0, hrv=30.0, state="HYPER", dt_seconds=0.5)
        for _i in range(60):
            tracker.accumulate(hr=70.0, hrv=50.0, state="FLOW", dt_seconds=0.5)

        difficulty = tracker.get_topic_difficulty("algorithms")
        assert difficulty is not None
        assert difficulty > 0.5  # More HYPER than FLOW = hard

    def test_topic_stress_modifier(self):
        from cortex.services.state_engine.longitudinal import LongitudinalTracker

        tracker = LongitudinalTracker()
        # No data → default 1.0
        assert tracker.get_topic_stress_modifier("unknown") == 1.0
