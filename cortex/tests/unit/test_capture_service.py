"""
Tests for Capture Service (services/capture_service/).

Tests cover:
- WebcamCapture: threaded capture, frame queuing, start/stop lifecycle
- FaceTracker: landmark extraction, hysteresis, bounding box, confidence
- FrameQualityScorer: brightness, blur, motion scoring, composite gate
- AdaptiveFrameSkipper: skip logic, latency adaptation
- CapturePipeline: integration of webcam → face → quality → output

All tests use synthetic/mock frames — no real webcam required.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from cortex.libs.config.settings import CaptureConfig
from cortex.services.capture_service.face_tracker import (
    BoundingBox,
    FaceTracker,
    FaceTrackingResult,
)
from cortex.services.capture_service.pipeline import AdaptiveFrameSkipper
from cortex.services.capture_service.quality import FrameQuality, FrameQualityScorer
from cortex.services.capture_service.webcam import CapturedFrame, WebcamCapture


# =============================================================================
# Helpers
# =============================================================================


def make_synthetic_frame(
    width: int = 640,
    height: int = 480,
    brightness: int = 128,
    noise: bool = True,
) -> np.ndarray:
    """Create a synthetic BGR frame with controllable brightness and noise."""
    frame = np.full((height, width, 3), brightness, dtype=np.uint8)
    if noise:
        # Add some texture so Laplacian variance isn't near-zero
        noise_arr = np.random.randint(0, 40, (height, width, 3), dtype=np.uint8)
        frame = cv2.add(frame, noise_arr)
    return frame


def make_dark_frame(width: int = 640, height: int = 480) -> np.ndarray:
    """Create a very dark frame (< 50 lux equivalent)."""
    return make_synthetic_frame(width, height, brightness=20, noise=False)


def make_bright_frame(width: int = 640, height: int = 480) -> np.ndarray:
    """Create an over-exposed frame."""
    return make_synthetic_frame(width, height, brightness=240, noise=False)


def make_blurry_frame(width: int = 640, height: int = 480) -> np.ndarray:
    """Create a blurry frame by heavy Gaussian smoothing."""
    frame = make_synthetic_frame(width, height, brightness=128)
    return cv2.GaussianBlur(frame, (31, 31), 10)


# =============================================================================
# FrameQualityScorer Tests
# =============================================================================


class TestFrameQualityScorer:
    """Tests for frame quality scoring along brightness, blur, motion axes."""

    def setup_method(self) -> None:
        self.scorer = FrameQualityScorer()

    def test_normal_frame_passes_quality(self) -> None:
        """A well-lit, sharp, stable frame should pass quality gate."""
        frame = make_synthetic_frame(brightness=128)
        quality = self.scorer.score(frame, nose_displacement=0.0)

        assert quality.brightness_score > 0.5
        assert quality.blur_score > 0.3
        assert quality.motion_score == 1.0
        assert quality.passed is True

    def test_dark_frame_low_brightness_score(self) -> None:
        """A very dark frame should have low brightness score."""
        frame = make_dark_frame()
        quality = self.scorer.score(frame)

        assert quality.brightness_score < 0.3
        # Dark frames may still pass if blur and motion are good
        # but brightness score itself should be low

    def test_bright_frame_reduced_brightness_score(self) -> None:
        """An over-exposed frame should have reduced brightness score."""
        frame = make_bright_frame()
        quality = self.scorer.score(frame)

        # Very bright but uniform — brightness penalized, blur may be low
        assert quality.brightness_score < 0.8

    def test_blurry_frame_low_blur_score(self) -> None:
        """A heavily blurred frame should have low blur score."""
        frame = make_blurry_frame()
        quality = self.scorer.score(frame)

        assert quality.blur_score < 0.5

    def test_sharp_frame_high_blur_score(self) -> None:
        """A sharp frame with texture should have high blur score."""
        # Create a frame with high-frequency content (edges)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Add checkerboard pattern for sharp edges
        for i in range(0, 480, 20):
            for j in range(0, 640, 20):
                if (i // 20 + j // 20) % 2 == 0:
                    frame[i : i + 20, j : j + 20] = 200
        quality = self.scorer.score(frame)
        assert quality.blur_score > 0.5

    def test_stable_motion_high_score(self) -> None:
        """Zero displacement means stable → high motion score."""
        frame = make_synthetic_frame()
        quality = self.scorer.score(frame, nose_displacement=0.0)
        assert quality.motion_score == 1.0

    def test_small_motion_still_high_score(self) -> None:
        """Small displacement (< half max_jitter) → still 1.0."""
        frame = make_synthetic_frame()
        quality = self.scorer.score(frame, nose_displacement=2.0)
        assert quality.motion_score == 1.0

    def test_excessive_motion_low_score(self) -> None:
        """Large displacement (> 2x max_jitter) → 0.0 motion score."""
        frame = make_synthetic_frame()
        quality = self.scorer.score(frame, nose_displacement=12.0)
        assert quality.motion_score == 0.0

    def test_moderate_motion_intermediate_score(self) -> None:
        """Moderate displacement → intermediate motion score."""
        frame = make_synthetic_frame()
        # max_jitter_px default is 5.0
        # Moderate: between 2.5 and 10.0
        quality = self.scorer.score(frame, nose_displacement=5.0)
        assert 0.0 < quality.motion_score < 1.0

    def test_quality_gate_rejects_excessive_motion(self) -> None:
        """Frame with excessive motion should fail quality gate."""
        frame = make_synthetic_frame()
        quality = self.scorer.score(frame, nose_displacement=15.0)
        assert quality.passed is False

    def test_all_scores_in_range(self) -> None:
        """All quality scores should be in [0.0, 1.0]."""
        for brightness in [0, 50, 128, 200, 255]:
            frame = make_synthetic_frame(brightness=brightness)
            for displacement in [0.0, 3.0, 7.0, 15.0]:
                quality = self.scorer.score(frame, nose_displacement=displacement)
                assert 0.0 <= quality.brightness_score <= 1.0
                assert 0.0 <= quality.blur_score <= 1.0
                assert 0.0 <= quality.motion_score <= 1.0


# =============================================================================
# FaceTracker Tests
# =============================================================================


class TestFaceTracker:
    """Tests for face tracking with hysteresis."""

    def _make_mock_landmarker(self, detect_result=None):
        """Create a mock FaceLandmarker with configurable results."""
        mock_landmarker = MagicMock()
        if detect_result is None:
            # Default: no face detected
            mock_result = MagicMock()
            mock_result.face_landmarks = []
            mock_landmarker.detect_for_video.return_value = mock_result
        else:
            mock_landmarker.detect_for_video.return_value = detect_result
        return mock_landmarker

    def _make_face_result_with_landmarks(self):
        """Create a mock FaceLandmarkerResult with synthetic face landmarks."""
        mock_result = MagicMock()
        # Create 478 landmarks with realistic face-like normalized coordinates
        landmarks = []
        for i in range(478):
            lm = MagicMock()
            lm.x = 0.3 + 0.4 * (i % 20) / 20  # x: 0.3-0.7
            lm.y = 0.2 + 0.5 * (i // 20) / 24  # y: 0.2-0.7
            lm.z = 0.03 + 0.02 * ((i % 5) / 5)  # z: 0.03-0.05
            landmarks.append(lm)
        mock_result.face_landmarks = [landmarks]
        return mock_result

    def test_initialization_and_release(self) -> None:
        """Tracker can be initialized and released without errors."""
        tracker = FaceTracker()
        # Mock the landmarker creation to avoid needing the model file
        mock_landmarker = self._make_mock_landmarker()
        with patch.object(tracker, '_model_path') as mock_path:
            mock_path.exists.return_value = True
            with patch("cortex.services.capture_service.face_tracker.mp.tasks.vision.FaceLandmarker") as MockFL:
                MockFL.create_from_options.return_value = mock_landmarker
                with patch("cortex.services.capture_service.face_tracker.mp.tasks.BaseOptions"):
                    with patch("cortex.services.capture_service.face_tracker.mp.tasks.vision.FaceLandmarkerOptions"):
                        tracker.initialize()
        assert tracker._landmarker is not None
        tracker.release()
        assert tracker._landmarker is None

    def test_process_before_init_raises(self) -> None:
        """Processing without initialization should raise RuntimeError."""
        tracker = FaceTracker()
        frame = make_synthetic_frame()
        with pytest.raises(RuntimeError, match="not initialized"):
            tracker.process_frame(frame)

    def test_no_face_returns_not_detected(self) -> None:
        """When MediaPipe detects no face, result should report face_detected=False."""
        tracker = FaceTracker()
        # Directly set the landmarker to a mock that returns no face
        tracker._landmarker = self._make_mock_landmarker()

        frame = make_synthetic_frame(brightness=128)
        result = tracker.process_frame(frame)
        assert result.face_detected is False
        assert result.confidence == 0.0
        assert result.landmarks is None
        assert result.bounding_box is None

    def test_face_detected_returns_landmarks(self) -> None:
        """When MediaPipe detects a face, result should have landmarks and bbox."""
        tracker = FaceTracker()
        face_result = self._make_face_result_with_landmarks()
        tracker._landmarker = MagicMock()
        tracker._landmarker.detect_for_video.return_value = face_result

        frame = make_synthetic_frame(brightness=128)
        result = tracker.process_frame(frame)
        assert result.face_detected is True
        assert result.confidence > 0.0
        assert result.landmarks is not None
        assert result.landmarks.shape == (478, 3)
        assert result.landmarks_px is not None
        assert result.landmarks_px.shape == (478, 2)
        assert result.bounding_box is not None
        assert result.face_stable is True

    def test_hysteresis_keeps_stable_during_brief_loss(self) -> None:
        """Face should remain 'stable' during brief detection gaps."""
        config = CaptureConfig(face_lost_tolerance_frames=3)
        tracker = FaceTracker(config)
        # Use mock landmarker that returns no face
        tracker._landmarker = self._make_mock_landmarker()

        # Simulate: face was previously detected
        tracker._face_detected_prev = True
        tracker._face_stable = True

        frame = make_synthetic_frame()

        # Frame 1: no face — should still be stable
        result = tracker.process_frame(frame)
        assert result.face_detected is False
        assert result.face_stable is True

        # Frame 2: still no face — still within tolerance
        result = tracker.process_frame(frame)
        assert result.face_stable is True

        # Frame 3: still no face — still within tolerance (3 frames)
        result = tracker.process_frame(frame)
        assert result.face_stable is True

        # Frame 4: no face — exceeded tolerance
        result = tracker.process_frame(frame)
        assert result.face_stable is False

    def test_hysteresis_resets_on_reacquire(self) -> None:
        """Hysteresis counter resets when face is reacquired."""
        config = CaptureConfig(face_lost_tolerance_frames=3)
        tracker = FaceTracker(config)

        # Simulate lost frames
        tracker._face_detected_prev = True
        tracker._face_lost_frames = 2

        # Simulate reacquire by calling _process_detected_face
        # Create fake landmarks (new Tasks API format: list of landmark objects)
        fake_landmarks = [
            MagicMock(x=0.5, y=0.5, z=0.03) for _ in range(478)
        ]
        result = tracker._process_detected_face(fake_landmarks, 480, 640)

        assert result.face_detected is True
        assert result.face_stable is True
        assert tracker._face_lost_frames == 0

    def test_bounding_box_properties(self) -> None:
        """BoundingBox properties compute correctly."""
        bbox = BoundingBox(x_min=100, y_min=50, x_max=300, y_max=250)
        assert bbox.width == 200
        assert bbox.height == 200
        assert bbox.center == (200, 150)

    def test_nose_tip_displacement_no_previous(self) -> None:
        """First frame should return 0.0 displacement."""
        tracker = FaceTracker()
        landmarks_px = np.random.rand(468, 2).astype(np.float32)
        assert tracker.compute_nose_tip_displacement(landmarks_px) == 0.0

    def test_nose_tip_displacement_with_previous(self) -> None:
        """Displacement should be Euclidean distance at nose tip (index 1)."""
        tracker = FaceTracker()

        prev = np.zeros((468, 2), dtype=np.float32)
        prev[1] = [100.0, 200.0]
        tracker._prev_landmarks_px = prev

        curr = np.zeros((468, 2), dtype=np.float32)
        curr[1] = [103.0, 204.0]  # 3,4,5 triangle → distance = 5.0

        displacement = tracker.compute_nose_tip_displacement(curr)
        assert abs(displacement - 5.0) < 0.01

    def test_get_landmark_subset(self) -> None:
        """Landmark subset extraction works correctly."""
        tracker = FaceTracker()
        landmarks = np.arange(468 * 3).reshape(468, 3).astype(np.float32)
        subset = tracker.get_landmark_subset(landmarks, [0, 10, 100])
        assert subset.shape == (3, 3)
        assert np.array_equal(subset[0], landmarks[0])
        assert np.array_equal(subset[1], landmarks[10])
        assert np.array_equal(subset[2], landmarks[100])


# =============================================================================
# AdaptiveFrameSkipper Tests
# =============================================================================


class TestAdaptiveFrameSkipper:
    """Tests for adaptive frame skip logic."""

    def test_no_skip_initially(self) -> None:
        """No frames should be skipped initially."""
        skipper = AdaptiveFrameSkipper(target_fps=30)
        assert skipper.should_skip(0) is False
        assert skipper.should_skip(1) is False
        assert skipper.current_skip_rate == 0

    def test_skip_increases_with_high_latency(self) -> None:
        """Skip rate should increase when processing is too slow."""
        skipper = AdaptiveFrameSkipper(target_fps=30)
        target_interval = 1.0 / 30  # ~33ms

        # Simulate consistently high processing latency (100ms > 33ms)
        for _ in range(20):
            skipper.update_latency(0.1)

        assert skipper.current_skip_rate > 0

    def test_skip_decreases_with_low_latency(self) -> None:
        """Skip rate should decrease when processing catches up."""
        skipper = AdaptiveFrameSkipper(target_fps=30)

        # First: high latency to trigger skipping
        for _ in range(20):
            skipper.update_latency(0.1)
        high_skip = skipper.current_skip_rate
        assert high_skip > 0

        # Then: low latency to reduce skipping
        for _ in range(50):
            skipper.update_latency(0.005)
        assert skipper.current_skip_rate < high_skip

    def test_skip_capped_at_5(self) -> None:
        """Skip rate should never exceed 5."""
        skipper = AdaptiveFrameSkipper(target_fps=30)

        # Extreme latency
        for _ in range(50):
            skipper.update_latency(1.0)

        assert skipper.current_skip_rate <= 5

    def test_skip_pattern(self) -> None:
        """When skip_count=1, every other frame should be skipped."""
        skipper = AdaptiveFrameSkipper(target_fps=30)
        skipper._skip_count = 1

        # With skip_count=1, frames are processed when sequence % 2 == 0
        results = [skipper.should_skip(i) for i in range(6)]
        # Frame 0: 0%2==0 → process (False)
        # Frame 1: 1%2==1 → skip (True)
        # Frame 2: 2%2==0 → process (False)
        # Frame 3: 3%2==1 → skip (True)
        assert results == [False, True, False, True, False, True]

    def test_total_skipped_counter(self) -> None:
        """Total skipped count should track correctly."""
        skipper = AdaptiveFrameSkipper(target_fps=30)
        skipper._skip_count = 2  # Skip 2 out of every 3

        for i in range(9):
            skipper.should_skip(i)

        # Frames 0,3,6 are processed (seq % 3 == 0)
        # Frames 1,2,4,5,7,8 are skipped = 6 skipped
        assert skipper.total_skipped == 6


# =============================================================================
# WebcamCapture Tests (with mocked cv2.VideoCapture)
# =============================================================================


class TestWebcamCapture:
    """Tests for WebcamCapture with mocked OpenCV."""

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self) -> None:
        """Capture can start and stop cleanly with a mock camera."""
        config = CaptureConfig(device_id=0, fps=30)
        capture = WebcamCapture(config)

        with patch("cortex.services.capture_service.webcam.cv2.VideoCapture") as MockCap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.read.return_value = (True, make_synthetic_frame())
            MockCap.return_value = mock_cap

            await capture.start()
            assert capture.is_running

            # Let it run briefly
            await asyncio.sleep(0.1)

            await capture.stop()
            assert not capture.is_running
            mock_cap.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_fails_if_camera_unavailable(self) -> None:
        """Start should raise RuntimeError if camera can't be opened."""
        capture = WebcamCapture()

        with patch("cortex.services.capture_service.webcam.cv2.VideoCapture") as MockCap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = False
            MockCap.return_value = mock_cap

            with pytest.raises(RuntimeError, match="Cannot open webcam"):
                await capture.start()

    @pytest.mark.asyncio
    async def test_frame_retrieval(self) -> None:
        """Frames should be retrievable from the async queue."""
        config = CaptureConfig(fps=30)
        capture = WebcamCapture(config)

        synthetic = make_synthetic_frame()
        with patch("cortex.services.capture_service.webcam.cv2.VideoCapture") as MockCap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.read.return_value = (True, synthetic)
            MockCap.return_value = mock_cap

            await capture.start()
            try:
                frame = await capture.get_frame(timeout=1.0)
                assert frame is not None
                assert isinstance(frame, CapturedFrame)
                assert frame.frame.shape == synthetic.shape
                assert frame.timestamp > 0
                assert frame.sequence >= 0
            finally:
                await capture.stop()

    @pytest.mark.asyncio
    async def test_get_frame_returns_none_when_stopped(self) -> None:
        """get_frame should return None when capture is not running."""
        capture = WebcamCapture()
        result = await capture.get_frame(timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_frame_nowait(self) -> None:
        """get_frame_nowait returns None when no frame available."""
        capture = WebcamCapture()
        assert capture.get_frame_nowait() is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self) -> None:
        """Calling start() twice should not error."""
        capture = WebcamCapture()

        with patch("cortex.services.capture_service.webcam.cv2.VideoCapture") as MockCap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.read.return_value = (True, make_synthetic_frame())
            MockCap.return_value = mock_cap

            await capture.start()
            await capture.start()  # Should be a no-op
            assert capture.is_running
            await capture.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_noop(self) -> None:
        """Calling stop() when already stopped should not error."""
        capture = WebcamCapture()
        await capture.stop()  # Should be a no-op

    @pytest.mark.asyncio
    async def test_metrics_tracking(self) -> None:
        """Metrics (frames_captured, frames_dropped) should update."""
        capture = WebcamCapture(queue_maxsize=5)

        with patch("cortex.services.capture_service.webcam.cv2.VideoCapture") as MockCap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.read.return_value = (True, make_synthetic_frame())
            MockCap.return_value = mock_cap

            await capture.start()
            await asyncio.sleep(0.3)  # Let frames accumulate
            await capture.stop()

            assert capture.frames_captured > 0


# =============================================================================
# CapturedFrame Tests
# =============================================================================


class TestCapturedFrame:
    """Tests for the CapturedFrame dataclass."""

    def test_frozen(self) -> None:
        """CapturedFrame should be immutable."""
        frame = CapturedFrame(
            frame=make_synthetic_frame(),
            timestamp=time.monotonic(),
            sequence=0,
        )
        with pytest.raises(AttributeError):
            frame.sequence = 1  # type: ignore[misc]

    def test_attributes(self) -> None:
        """CapturedFrame stores all expected attributes."""
        arr = make_synthetic_frame()
        ts = time.monotonic()
        frame = CapturedFrame(frame=arr, timestamp=ts, sequence=42)
        assert np.array_equal(frame.frame, arr)
        assert frame.timestamp == ts
        assert frame.sequence == 42
