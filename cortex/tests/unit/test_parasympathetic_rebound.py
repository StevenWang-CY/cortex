"""Unit tests for ParasympatheticReboundDetector."""

import time

from cortex.services.state_engine.parasympathetic_rebound import (
    ParasympatheticReboundDetector,
)

base_t = 1000.0


class TestParasympatheticReboundDetector:
    """Tests for the ParasympatheticReboundDetector."""

    def test_rebound_detected(self):
        """Rebound detected: accepted=True, HR within 5% baseline, HRV rising."""
        detector = ParasympatheticReboundDetector()
        result = detector.update(
            accepted=True,
            hr=71.0,          # within 5% of 70.0 (deviation ~1.4%)
            hr_baseline=70.0,
            hrv_current=55.0,  # rising from 48.0
            hrv_prev=48.0,
        )
        assert result is True
        assert detector.is_rebounding() is True

    def test_not_detected_when_not_accepted(self):
        """Not detected when accepted=False."""
        detector = ParasympatheticReboundDetector()
        result = detector.update(
            accepted=False,
            hr=70.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
        )
        assert result is False
        assert detector.is_rebounding() is False

    def test_not_detected_when_hr_too_far_from_baseline(self):
        """Not detected when HR deviates more than 5% from baseline."""
        detector = ParasympatheticReboundDetector()
        result = detector.update(
            accepted=True,
            hr=80.0,          # 14.3% deviation from 70.0 — too far
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
        )
        assert result is False
        assert detector.is_rebounding() is False

    def test_not_detected_when_hrv_not_rising(self):
        """Not detected when HRV is not rising (current <= prev)."""
        detector = ParasympatheticReboundDetector()
        result = detector.update(
            accepted=True,
            hr=70.0,
            hr_baseline=70.0,
            hrv_current=45.0,  # not rising — lower than prev
            hrv_prev=48.0,
        )
        assert result is False
        assert detector.is_rebounding() is False

    def test_reset_clears_state(self):
        """reset() clears the rebound flag."""
        detector = ParasympatheticReboundDetector()
        detector.update(
            accepted=True,
            hr=70.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
        )
        assert detector.is_rebounding() is True

        detector.reset()

        assert detector.is_rebounding() is False

    def test_handles_none_hr_and_hrv_gracefully(self):
        """Returns False (no crash) when hr or hrv values are None."""
        detector = ParasympatheticReboundDetector()

        # hr is None
        result = detector.update(
            accepted=True,
            hr=None,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
        )
        assert result is False

        # hrv_current is None
        result = detector.update(
            accepted=True,
            hr=70.0,
            hr_baseline=70.0,
            hrv_current=None,
            hrv_prev=48.0,
        )
        assert result is False

        # hrv_prev is None
        result = detector.update(
            accepted=True,
            hr=70.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=None,
        )
        assert result is False

    def test_rebound_rejected_20min_after_acceptance(self):
        """Rebound 20 min after last acceptance must return False (outside 5-min window)."""
        detector = ParasympatheticReboundDetector()
        twenty_min_ago = time.time() - 20 * 60  # 20 min ago
        result = detector.update(
            accepted=True,
            hr=71.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
            last_submission_ts=twenty_min_ago,
        )
        assert result is False
        assert detector.is_rebounding() is False

    def test_rebound_accepted_within_5min_of_acceptance(self):
        """Rebound 2 min after last acceptance must still pass."""
        detector = ParasympatheticReboundDetector()
        two_min_ago = time.time() - 2 * 60  # 2 min ago
        result = detector.update(
            accepted=True,
            hr=71.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
            last_submission_ts=two_min_ago,
        )
        assert result is True
        assert detector.is_rebounding() is True

    def test_rebound_without_timestamp_still_works(self):
        """When last_submission_ts is None, temporal check is skipped (backwards compat)."""
        detector = ParasympatheticReboundDetector()
        result = detector.update(
            accepted=True,
            hr=71.0,
            hr_baseline=70.0,
            hrv_current=55.0,
            hrv_prev=48.0,
            last_submission_ts=None,
        )
        assert result is True
