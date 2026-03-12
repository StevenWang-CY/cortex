"""
Unit tests for Kinematics Engine — Blink detector, head pose, posture.

Tests use synthetic landmarks to verify:
- EAR computation and blink detection state machine
- Blink rate and suppression score calculation
- Head pose estimation via solvePnP
- Posture analysis with face-only and full pose landmarks
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.config.settings import BlinkSignalConfig, LandmarksConfig, PostureSignalConfig
from cortex.services.kinematics_engine.blink_detector import BlinkDetector, BlinkState
from cortex.services.kinematics_engine.head_pose import HeadPoseEstimator, HeadPoseResult
from cortex.services.kinematics_engine.posture import PostureAnalyzer, PostureState


# =============================================================================
# Helpers — Synthetic Landmark Generation
# =============================================================================


def make_face_landmarks(
    n_landmarks: int = 478,
    frame_width: int = 640,
    frame_height: int = 480,
) -> np.ndarray:
    """
    Create synthetic face landmarks centered in the frame.

    Returns (478, 2) array with face-like coordinates.
    """
    rng = np.random.RandomState(42)
    landmarks = np.zeros((n_landmarks, 2), dtype=np.float32)

    # Center face in frame
    cx, cy = frame_width / 2, frame_height / 2

    # Place landmarks in a face-like distribution
    for i in range(n_landmarks):
        # Spread landmarks around center with face-shaped distribution
        landmarks[i, 0] = cx + rng.uniform(-80, 80)
        landmarks[i, 1] = cy + rng.uniform(-100, 100)

    return landmarks


def make_eye_landmarks_open() -> np.ndarray:
    """
    Create 6 eye landmarks in open-eye configuration.

    Returns (6, 2) array: [p1_outer, p2_upper_outer, p3_upper_inner,
                            p4_inner, p5_lower_inner, p6_lower_outer]
    """
    # Open eye: vertical opening is significant
    return np.array(
        [
            [100.0, 200.0],  # p1: outer corner
            [115.0, 190.0],  # p2: upper outer
            [130.0, 188.0],  # p3: upper inner
            [145.0, 200.0],  # p4: inner corner
            [130.0, 212.0],  # p5: lower inner
            [115.0, 210.0],  # p6: lower outer
        ],
        dtype=np.float32,
    )


def make_eye_landmarks_closed() -> np.ndarray:
    """
    Create 6 eye landmarks in closed-eye configuration.

    Returns (6, 2) with minimal vertical opening.
    """
    # Closed eye: vertical opening is near zero
    return np.array(
        [
            [100.0, 200.0],  # p1: outer corner
            [115.0, 199.0],  # p2: upper outer (barely above center)
            [130.0, 199.0],  # p3: upper inner
            [145.0, 200.0],  # p4: inner corner
            [130.0, 201.0],  # p5: lower inner (barely below center)
            [115.0, 201.0],  # p6: lower outer
        ],
        dtype=np.float32,
    )


def make_full_landmarks_with_eyes(
    left_eye: np.ndarray,
    right_eye: np.ndarray,
    n_landmarks: int = 478,
) -> np.ndarray:
    """
    Create full face landmarks with specific eye landmark configurations.

    Inserts the given eye landmarks at the correct FaceMesh indices.
    """
    config = LandmarksConfig()
    landmarks = make_face_landmarks(n_landmarks)

    # Insert left eye landmarks at configured indices
    for i, idx in enumerate(config.left_eye):
        landmarks[idx] = left_eye[i]

    # Insert right eye landmarks at configured indices
    for i, idx in enumerate(config.right_eye):
        landmarks[idx] = right_eye[i]

    # Set up PnP landmarks in reasonable positions
    # Nose tip (1)
    landmarks[1] = [320.0, 260.0]
    # Chin (152)
    landmarks[152] = [320.0, 360.0]
    # Left eye outer (33)
    landmarks[33] = left_eye[0]
    # Right eye outer (263)
    landmarks[263] = right_eye[3]
    # Left mouth corner (61)
    landmarks[61] = [290.0, 320.0]
    # Right mouth corner (291)
    landmarks[291] = [350.0, 320.0]

    # FaceMesh ear landmarks for posture
    landmarks[234] = [220.0, 240.0]  # Left ear
    landmarks[454] = [420.0, 240.0]  # Right ear
    # Forehead (10)
    landmarks[10] = [320.0, 180.0]

    return landmarks


# =============================================================================
# Blink Detector Tests
# =============================================================================


class TestBlinkDetectorEAR:
    """Test Eye Aspect Ratio computation."""

    def test_open_eye_ear_above_threshold(self):
        """Open eye should have EAR above blink threshold (0.21)."""
        eye = make_eye_landmarks_open()
        ear = BlinkDetector.compute_ear(eye)
        assert ear > 0.21, f"Open eye EAR={ear:.3f} should be > 0.21"

    def test_closed_eye_ear_below_threshold(self):
        """Closed eye should have EAR below blink threshold."""
        eye = make_eye_landmarks_closed()
        ear = BlinkDetector.compute_ear(eye)
        assert ear < 0.21, f"Closed eye EAR={ear:.3f} should be < 0.21"

    def test_ear_symmetry(self):
        """EAR should be the same for horizontally mirrored eyes."""
        eye = make_eye_landmarks_open()

        # Mirror horizontally
        mirrored = eye.copy()
        mirrored[:, 0] = 2 * eye[:, 0].mean() - eye[:, 0]
        # Reorder to match mirrored eye convention
        # p1↔p4, p2↔p3, p5↔p6 swap positions
        mirrored_reordered = mirrored[[3, 2, 1, 0, 5, 4]]

        ear_orig = BlinkDetector.compute_ear(eye)
        ear_mirror = BlinkDetector.compute_ear(mirrored_reordered)

        assert abs(ear_orig - ear_mirror) < 0.01

    def test_ear_zero_horizontal_distance(self):
        """EAR should return 0 when horizontal distance is zero."""
        eye = np.array(
            [
                [100.0, 200.0],
                [100.0, 190.0],
                [100.0, 190.0],
                [100.0, 200.0],  # Same x as p1 — zero horizontal
                [100.0, 210.0],
                [100.0, 210.0],
            ],
            dtype=np.float32,
        )
        ear = BlinkDetector.compute_ear(eye)
        assert ear == 0.0

    def test_ear_range(self):
        """EAR should be a reasonable positive value for normal eyes."""
        eye = make_eye_landmarks_open()
        ear = BlinkDetector.compute_ear(eye)
        assert 0.0 < ear < 1.0, f"EAR={ear:.3f} should be in (0, 1)"


class TestBlinkDetection:
    """Test blink event detection state machine."""

    def _make_detector(self) -> BlinkDetector:
        """Create a blink detector with default config."""
        return BlinkDetector(
            blink_config=BlinkSignalConfig(
                ear_threshold=0.21,
                ear_recovery=0.25,
                min_frames=3,
            ),
        )

    def test_no_blink_when_eyes_open(self):
        """No blink events when eyes stay open."""
        detector = self._make_detector()
        open_eye = make_eye_landmarks_open()
        landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)

        for i in range(30):
            state = detector.update(landmarks, timestamp=i / 30.0)

        assert state.blink_count_60s == 0
        assert state.is_closed is False

    def test_single_blink_detection(self):
        """Detect a single blink event."""
        detector = self._make_detector()
        open_eye = make_eye_landmarks_open()
        closed_eye = make_eye_landmarks_closed()

        t = 0.0
        dt = 1 / 30.0

        # 10 frames open
        for _ in range(10):
            landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
            detector.update(landmarks, timestamp=t)
            t += dt

        # 4 frames closed (>= min_frames=3)
        for _ in range(4):
            landmarks = make_full_landmarks_with_eyes(closed_eye, closed_eye)
            state = detector.update(landmarks, timestamp=t)
            t += dt

        assert state.is_closed is True

        # Recovery: eyes open again
        for _ in range(5):
            landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
            state = detector.update(landmarks, timestamp=t)
            t += dt

        # Should have detected 1 blink
        assert state.blink_count_60s == 1
        assert state.is_closed is False

    def test_blink_too_short_not_detected(self):
        """Closure shorter than min_frames should not be a blink."""
        detector = self._make_detector()
        open_eye = make_eye_landmarks_open()
        closed_eye = make_eye_landmarks_closed()

        t = 0.0
        dt = 1 / 30.0

        # 10 frames open
        for _ in range(10):
            landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
            detector.update(landmarks, timestamp=t)
            t += dt

        # 2 frames closed (< min_frames=3)
        for _ in range(2):
            landmarks = make_full_landmarks_with_eyes(closed_eye, closed_eye)
            detector.update(landmarks, timestamp=t)
            t += dt

        # Recovery
        for _ in range(10):
            landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
            state = detector.update(landmarks, timestamp=t)
            t += dt

        assert state.blink_count_60s == 0

    def test_multiple_blinks(self):
        """Detect multiple sequential blinks."""
        detector = self._make_detector()
        open_eye = make_eye_landmarks_open()
        closed_eye = make_eye_landmarks_closed()

        t = 0.0
        dt = 1 / 30.0
        n_blinks = 5

        for _ in range(n_blinks):
            # Open for a bit
            for _ in range(10):
                landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
                detector.update(landmarks, timestamp=t)
                t += dt

            # Close for min_frames
            for _ in range(4):
                landmarks = make_full_landmarks_with_eyes(closed_eye, closed_eye)
                detector.update(landmarks, timestamp=t)
                t += dt

            # Recovery
            for _ in range(5):
                landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)
                state = detector.update(landmarks, timestamp=t)
                t += dt

        assert state.blink_count_60s == n_blinks


class TestBlinkRate:
    """Test blink rate and suppression score computation."""

    def _make_detector_with_blinks(
        self, n_blinks: int, duration_seconds: float = 60.0
    ) -> tuple[BlinkDetector, BlinkState]:
        """Create detector and simulate n_blinks over duration."""
        detector = BlinkDetector(baseline_blink_rate=17.0)
        open_eye = make_eye_landmarks_open()
        closed_eye = make_eye_landmarks_closed()

        fps = 30.0
        total_frames = int(duration_seconds * fps)
        frames_per_blink = total_frames // max(n_blinks, 1)

        t = 0.0
        dt = 1 / fps
        state = None

        for frame_idx in range(total_frames):
            # Determine if we're in a blink phase
            phase = frame_idx % frames_per_blink if n_blinks > 0 else frames_per_blink
            is_blink_frame = n_blinks > 0 and 5 <= phase < 9  # 4 frames closed

            if is_blink_frame:
                landmarks = make_full_landmarks_with_eyes(closed_eye, closed_eye)
            else:
                landmarks = make_full_landmarks_with_eyes(open_eye, open_eye)

            state = detector.update(landmarks, timestamp=t)
            t += dt

        return detector, state

    def test_blink_rate_normal(self):
        """~15 blinks in 60s should give ~15 blinks/min."""
        _, state = self._make_detector_with_blinks(15, 60.0)
        assert state.blink_rate is not None
        assert 10.0 < state.blink_rate < 25.0, f"Rate={state.blink_rate}"

    def test_blink_suppression_low_rate(self):
        """Very few blinks should have high suppression score."""
        _, state = self._make_detector_with_blinks(3, 60.0)
        assert state.blink_rate is not None
        assert state.blink_suppression_score > 0.3, (
            f"Suppression={state.blink_suppression_score} for rate={state.blink_rate}"
        )

    def test_blink_suppression_normal_rate(self):
        """Normal blink rate should have low/zero suppression."""
        _, state = self._make_detector_with_blinks(18, 60.0)
        assert state.blink_rate is not None
        assert state.blink_suppression_score < 0.3

    def test_blink_rate_delta(self):
        """Blink rate delta should reflect difference from baseline."""
        detector = BlinkDetector(baseline_blink_rate=17.0)
        _, state = self._make_detector_with_blinks(5, 60.0)
        # With ~5 blinks/min vs baseline 17, delta should be negative
        assert state.blink_rate_delta is not None
        assert state.blink_rate_delta < 0.0

    def test_get_blink_features(self):
        """get_blink_features should return proper dict."""
        detector, _ = self._make_detector_with_blinks(10, 30.0)
        features = detector.get_blink_features()
        assert "blink_rate" in features
        assert "blink_rate_delta" in features
        assert "blink_suppression_score" in features

    def test_reset_clears_state(self):
        """Reset should clear all blink history."""
        detector, _ = self._make_detector_with_blinks(10, 30.0)
        detector.reset()
        assert detector.latest_state is None
        features = detector.get_blink_features()
        assert features["blink_rate"] is None


# =============================================================================
# Head Pose Estimator Tests
# =============================================================================


class TestHeadPoseEstimator:
    """Test head pose estimation."""

    def _make_estimator(self) -> HeadPoseEstimator:
        return HeadPoseEstimator(frame_width=640, frame_height=480)

    def _make_front_facing_landmarks(self) -> np.ndarray:
        """Create landmarks for a roughly front-facing head."""
        landmarks = make_face_landmarks()

        # Set PnP key landmarks for front-facing
        landmarks[1] = [320.0, 260.0]  # Nose tip — center
        landmarks[152] = [320.0, 370.0]  # Chin — below nose
        landmarks[33] = [250.0, 230.0]  # Left eye outer
        landmarks[263] = [390.0, 230.0]  # Right eye outer
        landmarks[61] = [285.0, 320.0]  # Left mouth
        landmarks[291] = [355.0, 320.0]  # Right mouth

        return landmarks

    def test_front_facing_near_zero_angles(self):
        """Front-facing head should have near-zero pitch/yaw."""
        estimator = self._make_estimator()
        landmarks = self._make_front_facing_landmarks()

        result = estimator.update(landmarks)

        # Front-facing should have relatively small angles
        assert abs(result.yaw) < 30.0, f"Yaw={result.yaw:.1f} too large for front-facing"
        assert isinstance(result.pitch, float)
        assert isinstance(result.roll, float)

    def test_turned_head_yaw(self):
        """Head turned to the side should show significant yaw change."""
        estimator = self._make_estimator()

        # Front facing first
        front = self._make_front_facing_landmarks()
        result_front = estimator.update(front)

        # Shift landmarks to simulate head turn (nose moves laterally)
        turned = front.copy()
        turned[1, 0] += 40.0  # Nose tip moves right
        turned[33, 0] += 30.0  # Left eye moves right
        turned[263, 0] += 50.0  # Right eye moves more right
        turned[61, 0] += 35.0
        turned[291, 0] += 45.0
        result_turned = estimator.update(turned)

        # Yaw should differ between front and turned
        yaw_diff = abs(result_turned.yaw - result_front.yaw)
        assert yaw_diff > 1.0, f"Yaw difference={yaw_diff:.1f} too small for head turn"

    def test_angular_velocity_on_movement(self):
        """Angular velocity should be positive when head moves."""
        estimator = self._make_estimator()

        landmarks = self._make_front_facing_landmarks()
        estimator.update(landmarks, timestamp=0.0)

        # Move landmarks
        moved = landmarks.copy()
        moved[1, 0] += 30.0  # Shift nose
        moved[1, 1] -= 20.0
        result = estimator.update(moved, timestamp=0.033)

        assert result.angular_velocity > 0.0

    def test_angular_velocity_zero_when_static(self):
        """Angular velocity should be near zero for static head."""
        estimator = self._make_estimator()

        landmarks = self._make_front_facing_landmarks()
        estimator.update(landmarks, timestamp=0.0)
        result = estimator.update(landmarks, timestamp=0.033)

        assert result.angular_velocity < 0.5

    def test_jitter_detection(self):
        """Rapid pose changes should trigger jitter detection."""
        estimator = HeadPoseEstimator(
            frame_width=640, frame_height=480, jitter_threshold_deg=2.0
        )

        landmarks = self._make_front_facing_landmarks()
        estimator.update(landmarks, timestamp=0.0)

        # Large sudden movement
        moved = landmarks.copy()
        moved[1] += [50.0, -40.0]  # Big nose shift
        moved[33] += [40.0, -30.0]
        moved[263] += [60.0, -30.0]
        moved[61] += [45.0, -10.0]
        moved[291] += [55.0, -10.0]
        moved[152] += [50.0, -20.0]
        result = estimator.update(moved, timestamp=0.033)

        assert result.is_jittery is True

    def test_freeze_detection(self):
        """No movement for extended period should trigger freeze."""
        estimator = HeadPoseEstimator(
            frame_width=640,
            frame_height=480,
            freeze_window_frames=10,  # Small window for testing
            freeze_threshold_deg=0.5,
        )

        landmarks = self._make_front_facing_landmarks()

        # Feed identical landmarks for many frames
        result = None
        for i in range(15):
            result = estimator.update(landmarks, timestamp=i / 30.0)

        assert result is not None
        assert result.is_frozen is True

    def test_get_head_pose_features(self):
        """get_head_pose_features should return proper dict."""
        estimator = self._make_estimator()
        landmarks = self._make_front_facing_landmarks()
        estimator.update(landmarks)

        features = estimator.get_head_pose_features()
        assert "head_pitch" in features
        assert "head_yaw" in features
        assert "head_roll" in features
        assert features["head_pitch"] is not None

    def test_get_head_pose_features_no_data(self):
        """Features should be None when no frames processed."""
        estimator = self._make_estimator()
        features = estimator.get_head_pose_features()
        assert features["head_pitch"] is None

    def test_reset(self):
        """Reset should clear all state."""
        estimator = self._make_estimator()
        landmarks = self._make_front_facing_landmarks()
        estimator.update(landmarks)
        estimator.reset()
        assert estimator.latest_result is None


# =============================================================================
# Posture Analyzer Tests
# =============================================================================


class TestPostureAnalyzerFace:
    """Test posture analysis with FaceMesh-only landmarks (fallback mode)."""

    def _make_analyzer(self) -> PostureAnalyzer:
        return PostureAnalyzer(config=PostureSignalConfig())

    def _make_upright_face_landmarks(self) -> np.ndarray:
        """Create face landmarks for upright posture."""
        landmarks = make_face_landmarks()

        # Upright: forehead above nose above chin, ears at sides
        landmarks[10] = [320.0, 160.0]   # Forehead — top
        landmarks[1] = [320.0, 260.0]    # Nose — middle
        landmarks[152] = [320.0, 360.0]  # Chin — bottom
        landmarks[234] = [220.0, 230.0]  # Left ear
        landmarks[454] = [420.0, 230.0]  # Right ear

        return landmarks

    def _make_leaning_face_landmarks(self, lean_deg: float = 25.0) -> np.ndarray:
        """Create face landmarks for forward lean posture."""
        landmarks = self._make_upright_face_landmarks()

        # Forward lean: rotate the forehead-chin axis
        # In image coords, forward lean tilts the face so chin moves forward
        # This shifts the chin horizontally relative to forehead
        angle_rad = np.radians(lean_deg)

        # Pivot around the center of the face
        cx, cy = 320.0, 260.0

        # Rotate forehead and chin around center
        for idx in [10, 152]:
            dx = landmarks[idx, 0] - cx
            dy = landmarks[idx, 1] - cy
            new_dx = dx * np.cos(angle_rad) - dy * np.sin(angle_rad)
            new_dy = dx * np.sin(angle_rad) + dy * np.cos(angle_rad)
            landmarks[idx] = [cx + new_dx, cy + new_dy]

        return landmarks

    def test_upright_low_lean(self):
        """Upright posture should have low forward lean."""
        analyzer = self._make_analyzer()
        landmarks = self._make_upright_face_landmarks()

        state = analyzer.update_with_face(landmarks, timestamp=0.0)

        assert state.forward_lean_angle is not None
        assert state.forward_lean_angle < 10.0, (
            f"Upright lean={state.forward_lean_angle:.1f} should be < 10°"
        )
        assert state.slump_score < 0.3

    def test_leaning_high_lean(self):
        """Forward leaning posture should have higher lean score."""
        analyzer = self._make_analyzer()
        upright = self._make_upright_face_landmarks()
        leaning = self._make_leaning_face_landmarks(25.0)

        state_upright = analyzer.update_with_face(upright, timestamp=0.0)
        state_leaning = analyzer.update_with_face(leaning, timestamp=0.033)

        assert state_leaning.forward_lean_angle > state_upright.forward_lean_angle
        assert state_leaning.forward_lean_score > state_upright.forward_lean_score

    def test_posture_collapse_on_big_lean(self):
        """Big forward lean should trigger posture collapse."""
        analyzer = self._make_analyzer()
        landmarks = self._make_leaning_face_landmarks(30.0)

        state = analyzer.update_with_face(landmarks, timestamp=0.0)

        # With 30° lean and threshold of 20°, should be collapsed
        assert state.is_collapsed is True

    def test_no_shoulder_drop_in_face_mode(self):
        """Face-only mode should not provide shoulder drop."""
        analyzer = self._make_analyzer()
        landmarks = self._make_upright_face_landmarks()

        state = analyzer.update_with_face(landmarks)

        assert state.shoulder_drop_ratio is None
        assert state.has_pose_landmarks is False

    def test_get_posture_features(self):
        """get_posture_features should return proper dict."""
        analyzer = self._make_analyzer()
        landmarks = self._make_upright_face_landmarks()
        analyzer.update_with_face(landmarks)

        features = analyzer.get_posture_features()
        assert "slump_score" in features
        assert "forward_lean_score" in features
        assert "shoulder_drop_ratio" in features

    def test_get_posture_features_no_data(self):
        """Features should be None when no frames processed."""
        analyzer = self._make_analyzer()
        features = analyzer.get_posture_features()
        assert features["slump_score"] is None


class TestPostureAnalyzerPose:
    """Test posture analysis with full Pose landmarks."""

    def _make_analyzer(self) -> PostureAnalyzer:
        return PostureAnalyzer(config=PostureSignalConfig())

    def _make_pose_landmarks(
        self, shoulder_y: float = 300.0, shoulder_spread: float = 200.0
    ) -> np.ndarray:
        """Create synthetic pose landmarks (33 landmarks)."""
        landmarks = np.zeros((33, 2), dtype=np.float32)

        # Shoulders (indices 11, 12)
        landmarks[11] = [220.0, shoulder_y]  # Left shoulder
        landmarks[12] = [220.0 + shoulder_spread, shoulder_y]  # Right shoulder

        return landmarks

    def test_auto_calibration(self):
        """Analyzer should auto-calibrate after 30 frames."""
        analyzer = self._make_analyzer()
        pose = self._make_pose_landmarks(shoulder_y=300.0)

        assert not analyzer.is_calibrated

        for i in range(35):
            analyzer.update_with_pose(pose, timestamp=i / 30.0)

        assert analyzer.is_calibrated

    def test_shoulder_drop_detection(self):
        """Shoulder drop should be detected after calibration."""
        analyzer = self._make_analyzer()

        # Calibrate with normal posture
        normal = self._make_pose_landmarks(shoulder_y=300.0)
        for i in range(35):
            analyzer.update_with_pose(normal, timestamp=i / 30.0)

        # Now simulate shoulder drop (shoulders move down = higher Y)
        dropped = self._make_pose_landmarks(shoulder_y=350.0)
        state = analyzer.update_with_pose(dropped, timestamp=1.5)

        assert state.shoulder_drop_ratio is not None
        assert state.shoulder_drop_ratio > 0.0

    def test_no_shoulder_drop_at_baseline(self):
        """No drop at baseline position."""
        analyzer = self._make_analyzer()
        pose = self._make_pose_landmarks(shoulder_y=300.0)

        # Calibrate
        for i in range(35):
            analyzer.update_with_pose(pose, timestamp=i / 30.0)

        # Same position — no drop
        state = analyzer.update_with_pose(pose, timestamp=1.5)

        assert state.shoulder_drop_ratio is not None
        assert state.shoulder_drop_ratio < 0.05

    def test_pose_mode_flag(self):
        """update_with_pose should set has_pose_landmarks=True."""
        analyzer = self._make_analyzer()
        pose = self._make_pose_landmarks()
        state = analyzer.update_with_pose(pose)
        assert state.has_pose_landmarks is True

    def test_manual_calibration(self):
        """calibrate_from_samples should set baseline."""
        analyzer = self._make_analyzer()
        analyzer.calibrate_from_samples(
            shoulder_y_values=[300.0, 305.0, 298.0],
            torso_lengths=[250.0, 255.0, 248.0],
        )
        assert analyzer.is_calibrated

    def test_reset_preserves_calibration(self):
        """reset() should preserve calibration."""
        analyzer = self._make_analyzer()
        analyzer.calibrate_from_samples(
            shoulder_y_values=[300.0],
            torso_lengths=[250.0],
        )
        analyzer.reset()
        assert analyzer.is_calibrated
        assert analyzer.latest_state is None

    def test_reset_calibration(self):
        """reset_calibration() should clear everything."""
        analyzer = self._make_analyzer()
        analyzer.calibrate_from_samples(
            shoulder_y_values=[300.0],
            torso_lengths=[250.0],
        )
        analyzer.reset_calibration()
        assert not analyzer.is_calibrated

    def test_smoothed_slump(self):
        """get_smoothed_slump should return a value."""
        analyzer = self._make_analyzer()
        pose = self._make_pose_landmarks()

        # Calibrate and add some data
        for i in range(35):
            analyzer.update_with_pose(pose, timestamp=i / 30.0)

        slump = analyzer.get_smoothed_slump()
        assert 0.0 <= slump <= 1.0


# =============================================================================
# Integration: Module Imports
# =============================================================================


class TestKinematicsEngineImports:
    """Test that all kinematics engine exports are importable."""

    def test_import_blink_detector(self):
        from cortex.services.kinematics_engine import BlinkDetector, BlinkEvent, BlinkState

        assert BlinkDetector is not None
        assert BlinkEvent is not None
        assert BlinkState is not None

    def test_import_head_pose(self):
        from cortex.services.kinematics_engine import HeadPoseEstimator, HeadPoseResult

        assert HeadPoseEstimator is not None
        assert HeadPoseResult is not None

    def test_import_posture(self):
        from cortex.services.kinematics_engine import PostureAnalyzer, PostureState

        assert PostureAnalyzer is not None
        assert PostureState is not None
