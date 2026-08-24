"""Tests for the non-medical, long-window respiration compatibility API."""

import numpy as np

from cortex.services.physio_engine.respiration import RespirationEstimator


class TestRespirationEstimator:
    def setup_method(self) -> None:
        self.estimator = RespirationEstimator(fs=30.0)

    @staticmethod
    def _respiratory_proxy(
        duration_s: float = 45.0,
        fs: float = 30.0,
        resp_hz: float = 0.25,
    ) -> np.ndarray:
        t = np.arange(0, duration_s, 1.0 / fs)
        return np.sin(2 * np.pi * resp_hz * t).astype(np.float64)

    def test_extracts_rate_from_adequate_proxy_window(self) -> None:
        signal = self._respiratory_proxy(resp_hz=0.25)
        estimate = self.estimator.process_bvp_window(signal)
        assert estimate.resp_rate_bpm is not None
        assert abs(estimate.resp_rate_bpm - 15.0) < 2.0
        assert estimate.confidence > 0.3
        assert estimate.unavailable_reason is None

    def test_window_under_thirty_seconds_abstains(self) -> None:
        signal = self._respiratory_proxy(duration_s=15.0)
        estimate = self.estimator.process_bvp_window(signal)
        assert estimate.resp_rate_bpm is None
        assert estimate.confidence == 0.0
        assert "30 seconds" in (estimate.unavailable_reason or "")

    def test_focus_argument_cannot_create_a_medical_event(self) -> None:
        signal = self._respiratory_proxy(resp_hz=0.10)
        estimate = self.estimator.process_bvp_window(
            signal,
            blink_suppression=1.0,
            timestamp=10_000.0,
        )
        assert not hasattr(estimate, "apnea_detected")

    def test_motion_proxy_is_used_when_explicitly_supplied(self) -> None:
        flat = np.ones(45 * 30, dtype=np.float64)
        motion = self._respiratory_proxy(resp_hz=0.20)
        estimate = self.estimator.process_bvp_window(
            flat, motion_proxy_signal=motion
        )
        assert estimate.resp_rate_bpm is not None
        assert abs(estimate.resp_rate_bpm - 12.0) < 2.0

    def test_latest_and_reset(self) -> None:
        assert self.estimator.latest_estimate is None
        self.estimator.process_bvp_window(self._respiratory_proxy())
        assert self.estimator.latest_estimate is not None
        self.estimator.reset()
        assert self.estimator.latest_estimate is None
