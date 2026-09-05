"""solvePnP head-pose convention, wrap-safe deltas and circular neutral pitch.

Audit defect D2: the 3D model was y-up / z-toward-camera while OpenCV camera
coordinates are y-down / z-away, so a frontal face solved to Rx(180 deg) and
pitch sat at +/-180 deg with a wrap through the neutral pose.  These tests
project the camera-convention model exactly and require pitch 0 for a
frontal face, positive pitch for flexion, continuity through neutral, a
circular neutral-pitch mean, and wrap-safe flexion in the posture proxy.
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.config.settings import PostureSignalConfig
from cortex.libs.signal.angles import circular_mean_deg, wrapped_angle_delta
from cortex.services.capture_service import calibration_runner
from cortex.services.kinematics_engine.head_pose import (
    _MODEL_POINTS_3D_CAMERA,
    _PNP_LANDMARK_INDICES,
    HeadPoseEstimator,
)
from cortex.services.kinematics_engine.posture import PostureAnalyzer

WIDTH, HEIGHT = 640, 480


def _rotation_x(pitch_deg: float) -> np.ndarray:
    theta = np.radians(pitch_deg)
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], dtype=np.float64)


def _landmarks_for_pitch(pitch_deg: float, *, distance_mm: float = 600.0) -> np.ndarray:
    """Exact pinhole projection of the camera-convention model at ``pitch_deg``.

    Uses the same intrinsics as :class:`HeadPoseEstimator` (focal length =
    frame width, principal point at the frame centre).
    """

    rotated = (_rotation_x(pitch_deg) @ _MODEL_POINTS_3D_CAMERA.T).T
    rotated[:, 2] += distance_mm
    focal = float(WIDTH)
    u = focal * rotated[:, 0] / rotated[:, 2] + WIDTH / 2.0
    v = focal * rotated[:, 1] / rotated[:, 2] + HEIGHT / 2.0
    landmarks = np.full((478, 2), np.nan, dtype=np.float64)
    landmarks[:, 0] = np.linspace(200.0, 440.0, 478)
    landmarks[:, 1] = np.linspace(150.0, 350.0, 478)
    landmarks[_PNP_LANDMARK_INDICES] = np.column_stack([u, v])
    return landmarks


def _solve(pitch_deg: float) -> tuple[float, float, float]:
    result = HeadPoseEstimator(frame_width=WIDTH, frame_height=HEIGHT).update(
        _landmarks_for_pitch(pitch_deg), 0.0
    )
    return result.pitch, result.yaw, result.roll


class TestSolvePnPConvention:
    def test_frontal_face_is_the_zero_pose(self) -> None:
        pitch, yaw, roll = _solve(0.0)
        assert pitch == pytest.approx(0.0, abs=0.5)
        assert yaw == pytest.approx(0.0, abs=0.5)
        assert roll == pytest.approx(0.0, abs=0.5)

    @pytest.mark.parametrize("expected", [-20.0, -10.0, 10.0, 20.0])
    def test_pitch_sign_and_magnitude_are_exact(self, expected: float) -> None:
        pitch, yaw, roll = _solve(expected)
        assert pitch == pytest.approx(expected, abs=0.5)
        assert yaw == pytest.approx(0.0, abs=0.5)
        assert roll == pytest.approx(0.0, abs=0.5)

    def test_looking_down_moves_the_chin_toward_the_nose_in_the_image(self) -> None:
        frontal = _landmarks_for_pitch(0.0)
        flexed = _landmarks_for_pitch(20.0)
        nose, chin = _PNP_LANDMARK_INDICES[0], _PNP_LANDMARK_INDICES[1]
        assert flexed[chin, 1] - flexed[nose, 1] < frontal[chin, 1] - frontal[nose, 1]
        pitch, _, _ = _solve(20.0)
        assert pitch > 0.0

    def test_pitch_is_continuous_through_neutral(self) -> None:
        estimator = HeadPoseEstimator(frame_width=WIDTH, frame_height=HEIGHT)
        sweep = np.arange(-20.0, 20.5, 2.0)
        pitches = [
            estimator.update(_landmarks_for_pitch(float(deg)), 0.1 * (index + 1)).pitch
            for index, deg in enumerate(sweep)
        ]
        steps = np.diff(pitches)
        assert bool((steps > 0).all())
        assert float(np.max(np.abs(steps - 2.0))) < 0.5
        assert not any(abs(value) > 90.0 for value in pitches)


class TestCircularStatistics:
    def test_wrapped_delta(self) -> None:
        assert wrapped_angle_delta(-179.0, 179.0) == pytest.approx(2.0)
        assert wrapped_angle_delta(179.0, -179.0) == pytest.approx(-2.0)
        assert wrapped_angle_delta(10.0, 5.0) == pytest.approx(5.0)

    def test_circular_mean_across_the_wrap(self) -> None:
        mean = circular_mean_deg([179.5, -179.5])
        assert abs(mean) == pytest.approx(180.0, abs=1e-6)
        assert circular_mean_deg([10.0, 20.0]) == pytest.approx(15.0)
        assert circular_mean_deg(np.asarray([170.0, -170.0, 175.0, -175.0])) == pytest.approx(
            180.0, abs=1e-6
        ) or circular_mean_deg(np.asarray([170.0, -170.0, 175.0, -175.0])) == pytest.approx(
            -180.0, abs=1e-6
        )
        with pytest.raises(ValueError):
            circular_mean_deg([])

    def test_calibration_neutral_pitch_distribution_is_circular(self) -> None:
        samples = [179.5, -179.5, 179.0, -179.0, 180.0]
        distribution = calibration_runner._circular_distribution_deg(samples)
        assert distribution is not None
        assert abs(distribution.mean) == pytest.approx(180.0, abs=0.1)
        assert distribution.std < 1.0
        assert distribution.p10 <= distribution.median <= distribution.p90
        assert distribution.p90 - distribution.p10 < 2.0
        linear = calibration_runner._distribution(samples)
        assert linear is not None
        assert abs(linear.mean) < 90.0  # the defect: linear mean lands near zero


class TestPostureWrap:
    def test_flexion_is_compared_through_the_wrap(self) -> None:
        analyzer = PostureAnalyzer(PostureSignalConfig())
        analyzer.apply_calibration(
            neutral_pitch_deg=179.0,
            neutral_face_scale=180.0,
            camera_identity_key="cam",
        )
        state = analyzer.update(
            pitch_deg=-179.0, face_scale=180.0, timestamp=1.0, camera_identity_key="cam"
        )
        assert state.head_neck_flexion_angle == pytest.approx(2.0)
        extension = analyzer.update(
            pitch_deg=170.0, face_scale=180.0, timestamp=2.0, camera_identity_key="cam"
        )
        assert extension.head_neck_flexion_angle == 0.0
