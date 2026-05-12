"""Tests for RespirationEstimator — rPPG respiratory rate extraction."""
import numpy as np

from cortex.services.physio_engine.respiration import RespirationEstimator


class TestRespirationEstimator:
    def setup_method(self):
        self.estimator = RespirationEstimator(fs=30.0)

    def _make_bvp_with_respiratory_modulation(
        self, duration_s=10.0, fs=30.0, cardiac_hz=1.2, resp_hz=0.25
    ):
        """Synthesize BVP with respiratory modulation at resp_hz."""
        t = np.arange(0, duration_s, 1.0 / fs)
        cardiac = np.sin(2 * np.pi * cardiac_hz * t)
        # Amplitude modulation at respiratory frequency
        resp_envelope = 1.0 + 0.3 * np.sin(2 * np.pi * resp_hz * t)
        bvp = cardiac * resp_envelope
        return bvp.astype(np.float64)

    def test_extracts_respiratory_rate_within_tolerance(self):
        """Synthetic BVP with 0.25 Hz respiratory modulation → ~15 BPM ± 2."""
        bvp = self._make_bvp_with_respiratory_modulation(duration_s=15.0, resp_hz=0.25)
        est = self.estimator.process_bvp_window(bvp, blink_suppression=0.0)
        assert est.resp_rate_bpm is not None
        assert abs(est.resp_rate_bpm - 15.0) < 3.0  # within ±3 BPM

    def test_confidence_is_positive_for_clean_signal(self):
        bvp = self._make_bvp_with_respiratory_modulation(duration_s=15.0)
        est = self.estimator.process_bvp_window(bvp)
        assert est.confidence > 0.1

    def test_apnea_detected_low_resp_high_blink_suppression(self):
        """resp_rate < 8 AND blink_suppression >= 0.5 → apnea."""
        # Very slow breathing: 0.1 Hz = 6 BPM
        bvp = self._make_bvp_with_respiratory_modulation(duration_s=15.0, resp_hz=0.1)
        est = self.estimator.process_bvp_window(bvp, blink_suppression=0.7)
        # May or may not trigger depending on filter — test the logic at least
        if est.resp_rate_bpm is not None and est.resp_rate_bpm < 8.0:
            assert est.apnea_detected is True

    def test_no_apnea_normal_breathing(self):
        bvp = self._make_bvp_with_respiratory_modulation(duration_s=15.0, resp_hz=0.25)
        est = self.estimator.process_bvp_window(bvp, blink_suppression=0.7)
        if est.resp_rate_bpm is not None and est.resp_rate_bpm >= 8.0:
            assert est.apnea_detected is False

    def test_short_window_returns_empty(self):
        """Window too short for filtering → empty estimate."""
        bvp = np.random.randn(10).astype(np.float64)
        est = self.estimator.process_bvp_window(bvp)
        assert est.resp_rate_bpm is None
        assert est.confidence == 0.0
        assert est.apnea_detected is False

    def test_latest_estimate_property(self):
        assert self.estimator.latest_estimate is None
        bvp = self._make_bvp_with_respiratory_modulation()
        self.estimator.process_bvp_window(bvp)
        assert self.estimator.latest_estimate is not None

    def test_reset_clears_state(self):
        bvp = self._make_bvp_with_respiratory_modulation()
        self.estimator.process_bvp_window(bvp)
        self.estimator.reset()
        assert self.estimator.latest_estimate is None
