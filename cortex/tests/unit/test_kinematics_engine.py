"""Physical-time invariants for camera-derived kinematic proxies.

The production pipeline is scheduled in time, not frames. These tests use
the same physical traces at several capture rates so a configuration change,
load shedding, or a brief dropped frame cannot change the meaning of a blink,
head velocity, freeze dwell, or calibrated head/neck flexion dwell.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from cortex.libs.config.settings import (
    BlinkSignalConfig,
    LandmarksConfig,
    PostureSignalConfig,
)
from cortex.services.kinematics_engine.blink_detector import BlinkDetector
from cortex.services.kinematics_engine.head_pose import HeadPoseEstimator
from cortex.services.kinematics_engine.posture import PostureAnalyzer

FPS_CASES = (15.0, 24.0, 30.0, 60.0)


def _eye(*, closed: bool) -> np.ndarray:
    vertical = 1.0 if closed else 11.0
    return np.asarray(
        [
            [100.0, 200.0],
            [115.0, 200.0 - vertical],
            [130.0, 200.0 - vertical],
            [145.0, 200.0],
            [130.0, 200.0 + vertical],
            [115.0, 200.0 + vertical],
        ],
        dtype=np.float64,
    )


def _face(*, closed: bool = False) -> np.ndarray:
    rng = np.random.default_rng(42)
    landmarks = np.empty((478, 2), dtype=np.float64)
    landmarks[:, 0] = 320.0 + rng.uniform(-80.0, 80.0, len(landmarks))
    landmarks[:, 1] = 240.0 + rng.uniform(-100.0, 100.0, len(landmarks))
    landmarks[1] = [320.0, 260.0]
    landmarks[152] = [320.0, 360.0]
    landmarks[33] = [280.0, 225.0]
    landmarks[263] = [360.0, 225.0]
    landmarks[61] = [290.0, 320.0]
    landmarks[291] = [350.0, 320.0]
    # Eye indices overlap several solvePnP points. Set the full six-point
    # eye geometry last so the blink trace is internally consistent.
    config = LandmarksConfig()
    eye = _eye(closed=closed)
    landmarks[config.left_eye] = eye
    landmarks[config.right_eye] = eye + np.asarray([180.0, 0.0])
    return landmarks


def _blink_config(*, min_exposure: float = 1.0) -> BlinkSignalConfig:
    return BlinkSignalConfig(
        ear_threshold=0.21,
        ear_recovery=0.25,
        min_closed_ms=80.0,
        max_closed_ms=1000.0,
        min_valid_exposure_seconds=min_exposure,
        history_window_seconds=60.0,
        max_valid_gap_ms=250.0,
    )


def _run_blink_trace(fps: float, *, drop_inside_blink: bool = False):
    detector = BlinkDetector(
        blink_config=_blink_config(),
        baseline_blink_rate=17.0,
    )
    state = None
    times = np.arange(0.0, 20.0 + 0.5 / fps, 1.0 / fps)
    blink_starts = (2.0, 6.0, 10.0, 14.0, 18.0)
    for timestamp in times:
        in_blink = any(start <= timestamp < start + 0.16 for start in blink_starts)
        if drop_inside_blink and 10.04 <= timestamp <= 10.10:
            # No call is the normal scheduler-load-shedding case. The next
            # valid observation remains inside the bounded 250 ms gap.
            continue
        state = detector.update(_face(closed=in_blink), float(timestamp))
    assert state is not None
    return state


class TestEyeAspectRatio:
    def test_open_and_closed_geometry(self) -> None:
        assert BlinkDetector.compute_ear(_eye(closed=False)) > 0.21
        assert BlinkDetector.compute_ear(_eye(closed=True)) < 0.21

    def test_rejects_malformed_or_nonfinite_geometry(self) -> None:
        with pytest.raises(ValueError, match=r"finite \(6, 2\)"):
            BlinkDetector.compute_ear(np.zeros((5, 2)))
        eye = _eye(closed=False)
        eye[0, 0] = np.nan
        with pytest.raises(ValueError, match=r"finite \(6, 2\)"):
            BlinkDetector.compute_ear(eye)

    def test_degenerate_horizontal_axis_is_zero(self) -> None:
        eye = _eye(closed=False)
        eye[3] = eye[0]
        assert BlinkDetector.compute_ear(eye) == 0.0


class TestElapsedBlinkMetrics:
    @pytest.mark.parametrize("fps", FPS_CASES)
    def test_same_physical_trace_is_fps_invariant(self, fps: float) -> None:
        state = _run_blink_trace(fps)
        assert state.readiness == "ready"
        assert state.blink_count_60s == 5
        assert state.blink_rate == pytest.approx(15.0, abs=0.2)
        assert state.perclos_60s == pytest.approx(0.04, abs=0.012)
        assert state.mean_blink_duration_ms == pytest.approx(160.0, abs=70.0)
        assert state.valid_exposure_seconds == pytest.approx(20.0, abs=0.08)

    def test_rates_match_across_supported_capture_rates(self) -> None:
        states = [_run_blink_trace(fps) for fps in FPS_CASES]
        rates = [state.blink_rate for state in states]
        perclos = [state.perclos_60s for state in states]
        assert (
            max(float(value) for value in rates if value is not None)
            - min(float(value) for value in rates if value is not None)
            < 0.2
        )
        assert (
            max(float(value) for value in perclos if value is not None)
            - min(float(value) for value in perclos if value is not None)
            < 0.01
        )

    def test_dropped_frames_do_not_shorten_elapsed_blink(self) -> None:
        complete = _run_blink_trace(30.0)
        dropped = _run_blink_trace(30.0, drop_inside_blink=True)
        assert dropped.blink_count_60s == complete.blink_count_60s
        assert dropped.mean_blink_duration_ms == pytest.approx(
            complete.mean_blink_duration_ms,
            abs=1.0 / 30.0 * 1000.0,
        )
        assert dropped.valid_exposure_seconds == pytest.approx(complete.valid_exposure_seconds)

    def test_cold_start_without_events_remains_warming_up(self) -> None:
        detector = BlinkDetector(blink_config=_blink_config(min_exposure=15.0))
        state = None
        for timestamp in np.arange(0.0, 10.0, 1.0 / 30.0):
            state = detector.update(_face(), float(timestamp))
        assert state is not None
        assert state.readiness == "warming_up"
        assert state.blink_rate is None
        assert state.blink_rate_delta is None
        assert state.blink_suppression_score is None
        assert state.perclos_60s is None

    def test_missing_observations_add_no_exposure_or_blink(self) -> None:
        detector = BlinkDetector(blink_config=_blink_config(min_exposure=0.5))
        detector.update(_face(), 0.0)
        detector.update(_face(closed=True), 0.1)
        detector.observe_missing(0.2)
        detector.update(_face(), 0.4)
        state = detector.update(_face(), 0.5)
        assert state.blink_count_60s == 0
        assert state.valid_exposure_seconds == pytest.approx(0.2)
        assert state.readiness == "warming_up"

    def test_history_window_clips_exposure_by_time(self) -> None:
        detector = BlinkDetector(
            blink_config=_blink_config(min_exposure=1.0),
            history_window_seconds=2.0,
        )
        state = None
        for timestamp in np.arange(0.0, 5.01, 0.1):
            state = detector.update(_face(), float(timestamp))
        assert state is not None
        assert state.valid_exposure_seconds == pytest.approx(2.0)

    def test_timestamp_contract_is_strict(self) -> None:
        detector = BlinkDetector(blink_config=_blink_config())
        detector.update(_face(), 1.0)
        with pytest.raises(ValueError, match="strictly increasing"):
            detector.update(_face(), 1.0)
        with pytest.raises(ValueError, match="non-negative"):
            BlinkDetector(blink_config=_blink_config()).update(_face(), -1.0)

    def test_reset_clears_temporal_state(self) -> None:
        detector = BlinkDetector(blink_config=_blink_config())
        detector.update(_face(), 0.0)
        detector.update(_face(closed=True), 0.1)
        detector.reset()
        assert detector.latest_state is None
        state = detector.update(_face(), 0.0)
        assert state.valid_exposure_seconds == 0.0


def _pose_sequence(
    estimator: HeadPoseEstimator,
    poses: Iterator[tuple[float, float, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(estimator, "_solve_head_pose", lambda _points: next(poses))


class TestHeadPosePhysicalTime:
    @pytest.mark.parametrize("fps", FPS_CASES)
    def test_angular_velocity_is_degrees_per_second(
        self,
        fps: float,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        times = list(np.arange(0.0, 2.0 + 0.5 / fps, 1.0 / fps))
        estimator = HeadPoseEstimator(jitter_threshold_deg_per_s=20.0)
        _pose_sequence(
            estimator,
            iter((10.0 * t, 0.0, 0.0) for t in times),
            monkeypatch,
        )
        result = None
        for timestamp in times:
            result = estimator.update(_face(), float(timestamp))
        assert result is not None
        assert result.angular_velocity_deg_per_s == pytest.approx(10.0, abs=1e-7)
        assert result.is_jittery is False

    @pytest.mark.parametrize("fps", FPS_CASES)
    def test_freeze_uses_contiguous_elapsed_dwell(
        self,
        fps: float,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        times = list(np.arange(0.0, 3.0 + 0.5 / fps, 1.0 / fps))
        estimator = HeadPoseEstimator(
            freeze_threshold_deg=0.5,
            freeze_window_seconds=3.0,
        )
        _pose_sequence(estimator, iter((1.0, 2.0, 3.0) for _ in times), monkeypatch)
        result = None
        for timestamp in times:
            result = estimator.update(_face(), float(timestamp))
        assert result is not None
        assert result.is_frozen is True
        assert result.valid_history_seconds == pytest.approx(3.0, abs=1.0 / fps)

    def test_missing_sample_breaks_freeze_contiguity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        estimator = HeadPoseEstimator(
            freeze_window_seconds=1.0,
            max_valid_gap_ms=600.0,
        )
        _pose_sequence(estimator, iter([(0.0, 0.0, 0.0)] * 4), monkeypatch)
        estimator.update(_face(), 0.0)
        estimator.update(_face(), 0.5)
        estimator.observe_missing(0.75)
        estimator.update(_face(), 1.0)
        result = estimator.update(_face(), 1.5)
        assert result.is_frozen is False
        assert result.valid_history_seconds == pytest.approx(0.5)

    def test_angles_unwrap_at_180_degrees(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        estimator = HeadPoseEstimator(
            jitter_threshold_deg_per_s=10.0,
            max_valid_gap_ms=1100.0,
        )
        _pose_sequence(
            estimator,
            iter([(0.0, 179.0, 0.0), (0.0, -179.0, 0.0)]),
            monkeypatch,
        )
        estimator.update(_face(), 0.0)
        result = estimator.update(_face(), 1.0)
        assert result.angular_velocity_deg_per_s == pytest.approx(2.0)
        assert result.is_jittery is False

    def test_real_pnp_path_is_finite(self) -> None:
        result = HeadPoseEstimator().update(_face(), 0.0)
        assert np.isfinite([result.pitch, result.yaw, result.roll]).all()

    def test_timestamp_contract_and_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        estimator = HeadPoseEstimator()
        _pose_sequence(estimator, iter([(0.0, 0.0, 0.0)] * 3), monkeypatch)
        estimator.update(_face(), 1.0)
        with pytest.raises(ValueError, match="strictly increasing"):
            estimator.update(_face(), 1.0)
        estimator.reset()
        assert estimator.latest_result is None
        estimator.update(_face(), 0.0)


def _posture_config(*, dwell_seconds: float = 2.0) -> PostureSignalConfig:
    return PostureSignalConfig(
        head_neck_flexion_threshold_deg=20.0,
        head_neck_sustain_seconds=dwell_seconds,
        face_scale_change_tolerance=0.25,
        max_valid_gap_ms=250.0,
    )


class TestCalibratedHeadNeckProxy:
    def test_is_unavailable_before_explicit_calibration(self) -> None:
        analyzer = PostureAnalyzer(_posture_config())
        state = analyzer.update(
            pitch_deg=30.0,
            face_scale=100.0,
            timestamp=0.0,
            camera_identity_key="camera-a",
        )
        assert state.proxy_available is False
        assert state.head_neck_flexion_angle is None
        assert state.invalidated_reason == "calibration_required"
        assert state.shoulder_drop_ratio is None
        assert state.has_pose_landmarks is False

    def test_calibrated_value_is_camera_relative(self) -> None:
        analyzer = PostureAnalyzer(_posture_config())
        analyzer.apply_calibration(
            neutral_pitch_deg=5.0,
            neutral_face_scale=100.0,
            camera_identity_key="camera-a",
        )
        state = analyzer.update(
            pitch_deg=30.0,
            face_scale=100.0,
            timestamp=0.0,
            camera_identity_key="camera-a",
        )
        assert state.proxy_available is True
        assert state.head_neck_flexion_angle == pytest.approx(25.0)
        assert state.head_neck_flexion_score == pytest.approx(25.0 / 45.0)
        assert state.is_sustained is False

    @pytest.mark.parametrize("fps", FPS_CASES)
    def test_sustained_dwell_is_fps_invariant(self, fps: float) -> None:
        analyzer = PostureAnalyzer(_posture_config(dwell_seconds=2.0))
        analyzer.apply_calibration(
            neutral_pitch_deg=0.0,
            neutral_face_scale=100.0,
            camera_identity_key="camera-a",
        )
        result = None
        for timestamp in np.arange(0.0, 2.0 + 0.5 / fps, 1.0 / fps):
            result = analyzer.update(
                pitch_deg=25.0,
                face_scale=100.0,
                timestamp=float(timestamp),
                camera_identity_key="camera-a",
            )
        assert result is not None
        assert result.sustained_flexion_seconds == pytest.approx(2.0, abs=1.0 / fps)
        assert result.is_sustained is True

    def test_missing_observation_resets_dwell(self) -> None:
        analyzer = PostureAnalyzer(_posture_config(dwell_seconds=1.0))
        analyzer.apply_calibration(
            neutral_pitch_deg=0.0,
            neutral_face_scale=100.0,
            camera_identity_key="camera-a",
        )
        analyzer.update(
            pitch_deg=25.0,
            face_scale=100.0,
            timestamp=0.0,
            camera_identity_key="camera-a",
        )
        analyzer.observe_missing(0.5)
        state = analyzer.update(
            pitch_deg=25.0,
            face_scale=100.0,
            timestamp=0.75,
            camera_identity_key="camera-a",
        )
        assert state.sustained_flexion_seconds == 0.0
        assert state.is_sustained is False

    @pytest.mark.parametrize(
        ("camera_key", "scale", "reason"),
        [
            ("camera-b", 100.0, "camera_identity_changed"),
            ("camera-a", 130.0, "face_scale_changed"),
        ],
    )
    def test_camera_or_scale_change_invalidates_calibration(
        self,
        camera_key: str,
        scale: float,
        reason: str,
    ) -> None:
        analyzer = PostureAnalyzer(_posture_config())
        analyzer.apply_calibration(
            neutral_pitch_deg=0.0,
            neutral_face_scale=100.0,
            camera_identity_key="camera-a",
        )
        state = analyzer.update(
            pitch_deg=10.0,
            face_scale=scale,
            timestamp=0.0,
            camera_identity_key=camera_key,
        )
        assert state.proxy_available is False
        assert state.invalidated_reason == reason
        assert analyzer.is_calibrated is False

    def test_face_scale_is_geometry_based_and_finite(self) -> None:
        scale = PostureAnalyzer.face_scale(_face())
        assert scale > 0.0
        malformed = _face()
        malformed[0, 0] = np.nan
        with pytest.raises(ValueError, match="finite"):
            PostureAnalyzer.face_scale(malformed)

    def test_reset_preserves_calibration_but_reset_calibration_does_not(self) -> None:
        analyzer = PostureAnalyzer(_posture_config())
        analyzer.apply_calibration(
            neutral_pitch_deg=0.0,
            neutral_face_scale=100.0,
            camera_identity_key="camera-a",
        )
        analyzer.reset()
        assert analyzer.is_calibrated is True
        analyzer.reset_calibration()
        assert analyzer.is_calibrated is False
