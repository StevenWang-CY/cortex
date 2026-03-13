"""Unit tests for AmygdalaHijackDetector."""

import pytest

from cortex.services.state_engine.amygdala_hijack import AmygdalaHijackDetector

base_t = 1000.0


class TestAmygdalaHijackDetector:
    """Tests for the AmygdalaHijackDetector."""

    def test_aai_computation_with_known_inputs(self):
        """AAI = alpha*max(0, hr_delta) - beta*blink_delta + gamma*key_velocity."""
        detector = AmygdalaHijackDetector(
            alpha=0.4, beta=0.3, gamma=0.3, threshold=10.0
        )
        # hr_delta=10.0, blink_delta=-2.0, key_velocity=0.5
        # AAI = 0.4*max(0,10) - 0.3*(-2.0) + 0.3*0.5
        #     = 4.0 + 0.6 + 0.15 = 4.75
        aai = detector.update(
            hr_delta=10.0,
            blink_delta=-2.0,
            key_velocity=0.5,
            wa_timestamp=None,
            current_time=base_t,
        )
        assert aai == pytest.approx(4.75)

    def test_is_hijacked_true_when_above_threshold_and_within_wa_window(self):
        """is_hijacked() returns True when AAI > threshold AND within WA window."""
        detector = AmygdalaHijackDetector(
            alpha=0.4, beta=0.3, gamma=0.3, threshold=0.5, wa_window_s=5.0
        )
        wa_time = base_t - 2.0  # 2 seconds ago, within 5s window
        detector.update(
            hr_delta=10.0,
            blink_delta=-2.0,
            key_velocity=0.5,
            wa_timestamp=wa_time,
            current_time=base_t,
        )
        assert detector.is_hijacked() is True

    def test_is_hijacked_false_when_above_threshold_but_no_recent_wa(self):
        """is_hijacked() returns False when AAI > threshold but no recent WA."""
        detector = AmygdalaHijackDetector(
            alpha=0.4, beta=0.3, gamma=0.3, threshold=0.5, wa_window_s=5.0
        )
        # wa_timestamp=None means no recent WA
        detector.update(
            hr_delta=10.0,
            blink_delta=-2.0,
            key_velocity=0.5,
            wa_timestamp=None,
            current_time=base_t,
        )
        assert detector.is_hijacked() is False

    def test_is_hijacked_false_when_below_threshold_with_wa(self):
        """is_hijacked() returns False when AAI < threshold even with WA."""
        detector = AmygdalaHijackDetector(
            alpha=0.4, beta=0.3, gamma=0.3, threshold=100.0, wa_window_s=5.0
        )
        wa_time = base_t - 1.0
        detector.update(
            hr_delta=0.0,
            blink_delta=0.0,
            key_velocity=0.0,
            wa_timestamp=wa_time,
            current_time=base_t,
        )
        assert detector.is_hijacked() is False

    def test_reset_clears_state(self):
        """reset() clears history, AAI, and hijacked flag."""
        detector = AmygdalaHijackDetector(threshold=0.5, wa_window_s=5.0)
        detector.update(
            hr_delta=10.0,
            blink_delta=-2.0,
            key_velocity=0.8,
            wa_timestamp=base_t - 1.0,
            current_time=base_t,
        )
        assert detector.is_hijacked() is True

        detector.reset()

        assert detector.is_hijacked() is False
        assert detector._latest_aai == 0.0
        assert len(detector._history) == 0

    def test_history_deque_stores_scores(self):
        """The internal _history deque accumulates samples."""
        detector = AmygdalaHijackDetector(threshold=10.0)
        for i in range(5):
            detector.update(
                hr_delta=1.0,
                blink_delta=0.0,
                key_velocity=0.0,
                wa_timestamp=None,
                current_time=base_t + i,
            )
        assert len(detector._history) == 5
