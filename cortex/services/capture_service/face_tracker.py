"""
Capture Service — Face Tracking via MediaPipe FaceLandmarker

Provides face detection and 478-landmark extraction using MediaPipe's
FaceLandmarker Tasks API. Handles face lost/reacquire with hysteresis
and outputs normalized landmarks plus face bounding box and confidence.

Design:
- MediaPipe FaceLandmarker (Tasks API) with 478 landmarks
- Face lost/reacquire hysteresis (configurable tolerance, default 5 frames)
- Bounding box + confidence extraction
- Landmark normalization to [0, 1] range (provided by MediaPipe)
- No frame storage — all processing is ephemeral
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

from cortex.libs.config.settings import CaptureConfig

logger = logging.getLogger(__name__)

# Default model path relative to the cortex package root
_DEFAULT_MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "face_landmarker.task"


@dataclass(frozen=True)
class BoundingBox:
    """Face bounding box in pixel coordinates."""

    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @property
    def width(self) -> int:
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        return self.y_max - self.y_min

    @property
    def center(self) -> tuple[int, int]:
        return (self.x_min + self.x_max) // 2, (self.y_min + self.y_max) // 2


@dataclass(frozen=True)
class FaceTrackingResult:
    """Result of face tracking on a single frame."""

    face_detected: bool
    confidence: float  # 0.0 to 1.0
    landmarks: np.ndarray | None  # (N, 3) normalized x,y,z or None
    landmarks_px: np.ndarray | None  # (N, 2) pixel coordinates or None
    bounding_box: BoundingBox | None
    face_stable: bool  # True if face has been consistently detected (hysteresis passed)


class FaceTracker:
    """
    MediaPipe FaceLandmarker face tracker with hysteresis.

    Tracks a single face using MediaPipe's FaceLandmarker Tasks API.
    Implements face lost/reacquire hysteresis to prevent flickering when
    the face is momentarily lost.

    Usage:
        tracker = FaceTracker(config)
        tracker.initialize()
        result = tracker.process_frame(frame)
        tracker.release()
    """

    def __init__(
        self,
        config: CaptureConfig | None = None,
        model_path: str | Path | None = None,
    ) -> None:
        self._config = config or CaptureConfig()
        self._model_path = Path(model_path) if model_path else _DEFAULT_MODEL_PATH
        self._landmarker: mp.tasks.vision.FaceLandmarker | None = None
        self._frame_timestamp_ms = 0

        # Hysteresis state
        self._face_lost_frames = 0
        self._face_detected_prev = False
        self._face_stable = False

        # Previous landmarks for motion tracking
        self._prev_landmarks_px: np.ndarray | None = None

    def initialize(self) -> None:
        """
        Initialize MediaPipe FaceLandmarker.

        Raises:
            FileNotFoundError: If the model file is not found.
        """
        if not self._model_path.exists():
            raise FileNotFoundError(
                f"FaceLandmarker model not found at {self._model_path}. "
                "Download from: https://storage.googleapis.com/mediapipe-models/"
                "face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
            )

        base_options = mp.tasks.BaseOptions(
            model_asset_path=str(self._model_path)
        )
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        self._frame_timestamp_ms = 0
        logger.info("FaceTracker initialized with MediaPipe FaceLandmarker Tasks API")

    def release(self) -> None:
        """Release MediaPipe resources."""
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None
        self._prev_landmarks_px = None
        logger.info("FaceTracker released")

    def process_frame(self, frame: np.ndarray) -> FaceTrackingResult:
        """
        Process a single BGR frame and extract face landmarks.

        Args:
            frame: BGR uint8 image, shape (H, W, 3)

        Returns:
            FaceTrackingResult with landmarks, bounding box, and confidence.
        """
        if self._landmarker is None:
            raise RuntimeError("FaceTracker not initialized. Call initialize() first.")

        h, w = frame.shape[:2]

        # MediaPipe Tasks API expects RGB input via mp.Image
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        # Advance timestamp (must be monotonically increasing for VIDEO mode)
        self._frame_timestamp_ms += 33  # ~30fps
        result = self._landmarker.detect_for_video(mp_image, self._frame_timestamp_ms)

        if result.face_landmarks and len(result.face_landmarks) > 0:
            face_landmarks = result.face_landmarks[0]
            return self._process_detected_face(face_landmarks, h, w)
        else:
            return self._process_no_face()

    def _process_detected_face(
        self,
        face_landmarks: list,
        height: int,
        width: int,
    ) -> FaceTrackingResult:
        """Process a frame where a face was detected."""
        # Extract normalized landmarks (N x 3)
        landmarks = np.array(
            [[lm.x, lm.y, lm.z] for lm in face_landmarks],
            dtype=np.float32,
        )

        # Compute pixel coordinates (N x 2)
        n_landmarks = len(face_landmarks)
        landmarks_px = np.zeros((n_landmarks, 2), dtype=np.float32)
        landmarks_px[:, 0] = landmarks[:, 0] * width
        landmarks_px[:, 1] = landmarks[:, 1] * height

        # Compute bounding box from landmarks
        x_coords = landmarks_px[:, 0]
        y_coords = landmarks_px[:, 1]
        bbox = BoundingBox(
            x_min=max(0, int(x_coords.min())),
            y_min=max(0, int(y_coords.min())),
            x_max=min(width, int(x_coords.max())),
            y_max=min(height, int(y_coords.max())),
        )

        # Compute confidence from detection stability
        confidence = self._compute_confidence(landmarks)

        # Update hysteresis: face is found, reset lost counter
        self._face_lost_frames = 0
        self._face_detected_prev = True
        self._face_stable = True

        # Store for motion computation
        self._prev_landmarks_px = landmarks_px

        return FaceTrackingResult(
            face_detected=True,
            confidence=confidence,
            landmarks=landmarks,
            landmarks_px=landmarks_px,
            bounding_box=bbox,
            face_stable=True,
        )

    def _process_no_face(self) -> FaceTrackingResult:
        """Process a frame where no face was detected."""
        if self._face_detected_prev:
            self._face_lost_frames += 1

            # Hysteresis: keep reporting face as "stable" during tolerance window
            if self._face_lost_frames <= self._config.face_lost_tolerance_frames:
                return FaceTrackingResult(
                    face_detected=False,
                    confidence=0.0,
                    landmarks=None,
                    landmarks_px=None,
                    bounding_box=None,
                    face_stable=True,  # Still within tolerance
                )

            # Tolerance exceeded — face truly lost
            self._face_detected_prev = False
            self._face_stable = False
            self._prev_landmarks_px = None

        return FaceTrackingResult(
            face_detected=False,
            confidence=0.0,
            landmarks=None,
            landmarks_px=None,
            bounding_box=None,
            face_stable=False,
        )

    def _compute_confidence(self, landmarks: np.ndarray) -> float:
        """
        Compute face detection confidence from landmark quality.

        Uses the z-coordinate spread and face proportion as confidence proxies.

        Args:
            landmarks: (N, 3) normalized landmarks

        Returns:
            Confidence score 0.0 to 1.0
        """
        # Check landmark z-spread (lower = more reliable, face is flatter in z)
        z_spread = np.std(landmarks[:, 2])
        # Typical z_spread for a well-detected face is 0.02-0.06
        z_score = np.clip(1.0 - (z_spread - 0.02) / 0.08, 0.0, 1.0)

        # Check face proportion (width/height ratio should be ~0.7-0.9)
        x_range = landmarks[:, 0].max() - landmarks[:, 0].min()
        y_range = landmarks[:, 1].max() - landmarks[:, 1].min()
        if y_range < 1e-6:
            return 0.0
        aspect = x_range / y_range
        # Ideal aspect is ~0.8; penalize if too far off
        aspect_score = np.clip(1.0 - abs(aspect - 0.8) / 0.4, 0.0, 1.0)

        # Combine
        confidence = 0.6 * z_score + 0.4 * aspect_score
        return float(np.clip(confidence, 0.0, 1.0))

    def compute_nose_tip_displacement(self, current_landmarks_px: np.ndarray) -> float:
        """
        Compute inter-frame nose tip displacement in pixels.

        Used for motion quality gating (discard if > max_jitter_px).

        Args:
            current_landmarks_px: Current frame pixel landmarks (N, 2)

        Returns:
            Displacement in pixels, or 0.0 if no previous frame.
        """
        if self._prev_landmarks_px is None:
            return 0.0

        # Nose tip is landmark index 1 in MediaPipe FaceMesh
        nose_idx = 1
        prev_nose = self._prev_landmarks_px[nose_idx]
        curr_nose = current_landmarks_px[nose_idx]
        displacement = float(np.linalg.norm(curr_nose - prev_nose))
        return displacement

    def get_landmark_subset(
        self, landmarks: np.ndarray, indices: list[int]
    ) -> np.ndarray:
        """
        Extract a subset of landmarks by index.

        Args:
            landmarks: Full (N, 3) or (N, 2) landmark array
            indices: List of landmark indices to extract

        Returns:
            Subset array of shape (len(indices), D)
        """
        return landmarks[indices]
