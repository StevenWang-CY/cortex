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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import CaptureConfig

logger = logging.getLogger(__name__)

# audit Phase-I: ``mediapipe`` is imported lazily inside
# :func:`_ensure_mediapipe`. The module is heavyweight (>200 ms cold
# import on M-series) and we only need it once the capture pipeline is
# actually started, so deferring the import keeps daemon startup below
# 2 s. ``cortex/tests/performance/test_startup_latency.py`` is the
# regression guard.
#
# ``mp`` is exposed as a module-level attribute so existing tests can
# monkey-patch ``cortex.services.capture_service.face_tracker.mp`` to a
# stand-in (they did so against the eager-import shape); production code
# routes through :func:`_ensure_mediapipe` which performs the real
# import on first use.
mp: Any = None


def _ensure_mediapipe() -> Any:
    """Import ``mediapipe`` on first use and cache the module handle."""
    global mp
    if mp is None:
        import mediapipe as _mediapipe  # noqa: PLC0415 — intentional lazy import

        mp = _mediapipe
    return mp


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
    landmarks: NDArray[np.float32] | None  # (N, 3) normalized x,y,z or None
    landmarks_px: NDArray[np.float32] | None  # (N, 2) pixel coordinates or None
    bounding_box: BoundingBox | None
    face_stable: bool  # True if face has been consistently detected (hysteresis passed)
    # audit Phase-I: True when these landmarks are a byte-for-byte replay
    # of an earlier frame's mediapipe result (``face_mesh_subsample_n > 1``
    # skip path). Per-frame consumers that integrate over time — blink
    # duration (BlinkDetector hardcodes 1000/30 ms per frame) and head
    # angular velocity — MUST NOT re-process a replayed frame as if it
    # were a fresh measurement, or those rates are scaled by the subsample
    # factor. A fresh mediapipe detection always has ``is_replayed=False``.
    is_replayed: bool = False
    observed_at_mono_ns: int | None = None
    detector_timestamp_ms: int | None = None
    detector_timestamp_adjusted: bool = False
    nose_displacement_px: float = 0.0
    nose_velocity_px_per_second: float | None = None
    motion_face_widths_per_second: float | None = None
    sample_interval_ms: float | None = None


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
        self._landmarker: Any = None  # mediapipe FaceLandmarker (lazy import)
        self._frame_timestamp_ms = 0
        self._synthetic_capture_mono_ns = 0

        # Hysteresis state
        self._face_lost_frames = 0
        self._face_detected_prev = False
        self._face_stable = False
        self._last_face_seen_mono_ns: int | None = None

        # Previous landmarks for motion tracking
        self._prev_landmarks_px: NDArray[np.float32] | None = None
        self._prev_landmarks_mono_ns: int | None = None
        self._prev_face_width_px: float | None = None

        # audit Phase-I: cached result for sub-sampled frames. When
        # ``face_mesh_subsample_n > 1`` we run MediaPipe only every
        # ``n``-th frame and replay the most recent landmarks/bbox for
        # the frames we skip. ``_last_result`` is the cache slot;
        # ``_subsample_counter`` is the frame counter modulo ``n``.
        self._last_result: FaceTrackingResult | None = None
        self._subsample_counter = 0

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

        mp = _ensure_mediapipe()
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
        self._synthetic_capture_mono_ns = 0
        logger.info("FaceTracker initialized with MediaPipe FaceLandmarker Tasks API")

    def release(self) -> None:
        """Release MediaPipe resources."""
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None
        self._prev_landmarks_px = None
        self._prev_landmarks_mono_ns = None
        self._prev_face_width_px = None
        self._last_face_seen_mono_ns = None
        self._face_lost_frames = 0
        self._face_detected_prev = False
        self._face_stable = False
        # audit Phase-I: discard the sub-sample cache so a restart never
        # replays stale landmarks from a previous session.
        self._last_result = None
        self._subsample_counter = 0
        logger.info("FaceTracker released")

    @property
    def face_stable(self) -> bool:
        """Current time-hysteresis state for scheduled missing reads/skips."""

        return self._face_stable

    def process_missing(self, *, capture_mono_ns: int) -> FaceTrackingResult:
        """Advance face-loss time when the camera produced no frame."""

        return self._process_no_face(capture_mono_ns=capture_mono_ns)

    def process_frame(
        self,
        frame: NDArray[np.uint8],
        rgb_frame: NDArray[np.uint8] | None = None,
        *,
        capture_mono_ns: int | None = None,
    ) -> FaceTrackingResult:
        """
        Process a single BGR frame and extract face landmarks.

        Args:
            frame: BGR uint8 image, shape (H, W, 3).
            rgb_frame: Optional pre-converted RGB view of ``frame``. When
                supplied, the FaceTracker reuses it instead of calling
                ``cv2.cvtColor`` again — used by the audit Phase-I
                colour-convert cache so the BGR→RGB conversion runs
                exactly once per captured frame even when multiple
                detectors consume the same frame.

        Returns:
            FaceTrackingResult with landmarks, bounding box, and confidence.
        """
        if self._landmarker is None:
            raise RuntimeError("FaceTracker not initialized. Call initialize() first.")

        capture_ns = self._resolve_capture_mono_ns(capture_mono_ns)

        # audit Phase-I: sub-sample mediapipe at ``face_mesh_subsample_n``.
        # When the counter is not 0 we replay the cached result so
        # downstream consumers still receive a structurally-valid
        # FaceTrackingResult on every frame; mediapipe itself runs at
        # ``fps / subsample_n``. ``n=1`` (the legacy default) disables the
        # skip path entirely. The first frame always runs through
        # mediapipe so the cache is primed.
        subsample_n = max(1, self._config.face_mesh_subsample_n)
        if subsample_n > 1 and self._last_result is not None:
            self._subsample_counter = (self._subsample_counter + 1) % subsample_n
            if self._subsample_counter != 0:
                # Mark the replayed result so time-integrating consumers
                # (blink duration, angular velocity) can skip it instead of
                # double-counting stale landmarks as a fresh measurement.
                return replace(
                    self._last_result,
                    is_replayed=True,
                    observed_at_mono_ns=capture_ns,
                    nose_displacement_px=0.0,
                    nose_velocity_px_per_second=None,
                    motion_face_widths_per_second=None,
                    sample_interval_ms=None,
                )
        else:
            self._subsample_counter = 0

        h, w = frame.shape[:2]

        # MediaPipe Tasks API expects RGB input via mp.Image. The caller
        # may supply a cached RGB view (audit Phase-I colour-convert
        # cache) to avoid duplicating the cvtColor across detectors that
        # share the same frame.
        if rgb_frame is None:
            rgb_frame = cast(
                NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            )
        mp = _ensure_mediapipe()
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        # MediaPipe VIDEO mode requires a strictly increasing millisecond
        # timestamp. Use the real monotonic capture time; repeated/sub-ms
        # scheduler instants are clamped only for MediaPipe and surfaced in
        # the result so downstream can reject the ambiguous observation.
        raw_timestamp_ms = capture_ns // 1_000_000
        detector_timestamp_ms = max(raw_timestamp_ms, self._frame_timestamp_ms + 1)
        timestamp_adjusted = detector_timestamp_ms != raw_timestamp_ms
        self._frame_timestamp_ms = detector_timestamp_ms
        result = self._landmarker.detect_for_video(mp_image, detector_timestamp_ms)

        if result.face_landmarks and len(result.face_landmarks) > 0:
            face_landmarks = result.face_landmarks[0]
            tracking = self._process_detected_face(
                face_landmarks,
                h,
                w,
                capture_mono_ns=capture_ns,
                detector_timestamp_ms=detector_timestamp_ms,
                detector_timestamp_adjusted=timestamp_adjusted,
            )
        else:
            tracking = self._process_no_face(
                capture_mono_ns=capture_ns,
                detector_timestamp_ms=detector_timestamp_ms,
                detector_timestamp_adjusted=timestamp_adjusted,
            )

        self._last_result = tracking
        return tracking

    def _process_detected_face(
        self,
        face_landmarks: list[Any],
        height: int,
        width: int,
        *,
        capture_mono_ns: int | None = None,
        detector_timestamp_ms: int | None = None,
        detector_timestamp_adjusted: bool = False,
    ) -> FaceTrackingResult:
        """Process a frame where a face was detected."""
        capture_ns = self._resolve_capture_mono_ns(capture_mono_ns)
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

        # Derivatives MUST be computed against the previous committed sample
        # before current landmarks replace it. Normalize by face width and
        # elapsed monotonic time so the threshold is stable across resolution
        # and frame rate.
        displacement_px = self.compute_nose_tip_displacement(landmarks_px)
        sample_interval_ms: float | None = None
        velocity_px_s: float | None = None
        motion_face_widths_s: float | None = None
        if (
            self._prev_landmarks_px is not None
            and self._prev_landmarks_mono_ns is not None
            and capture_ns > self._prev_landmarks_mono_ns
        ):
            dt_s = (capture_ns - self._prev_landmarks_mono_ns) / 1_000_000_000.0
            sample_interval_ms = dt_s * 1000.0
            velocity_px_s = displacement_px / dt_s
            current_width = max(1.0, float(bbox.width))
            reference_width = (
                (current_width + self._prev_face_width_px) / 2.0
                if self._prev_face_width_px is not None
                else current_width
            )
            motion_face_widths_s = velocity_px_s / max(1.0, reference_width)
        elif self._prev_landmarks_px is None:
            velocity_px_s = 0.0
            motion_face_widths_s = 0.0

        # Update hysteresis: face is found, reset lost counter
        self._face_lost_frames = 0
        self._face_detected_prev = True
        self._face_stable = True
        self._last_face_seen_mono_ns = capture_ns

        # Commit only after derivative computation.
        self._prev_landmarks_px = landmarks_px.copy()
        self._prev_landmarks_mono_ns = capture_ns
        self._prev_face_width_px = max(1.0, float(bbox.width))

        return FaceTrackingResult(
            face_detected=True,
            confidence=confidence,
            landmarks=landmarks,
            landmarks_px=landmarks_px,
            bounding_box=bbox,
            face_stable=True,
            observed_at_mono_ns=capture_ns,
            detector_timestamp_ms=detector_timestamp_ms,
            detector_timestamp_adjusted=detector_timestamp_adjusted,
            nose_displacement_px=displacement_px,
            nose_velocity_px_per_second=velocity_px_s,
            motion_face_widths_per_second=motion_face_widths_s,
            sample_interval_ms=sample_interval_ms,
        )

    def _process_no_face(
        self,
        *,
        capture_mono_ns: int | None = None,
        detector_timestamp_ms: int | None = None,
        detector_timestamp_adjusted: bool = False,
    ) -> FaceTrackingResult:
        """Process a frame where no face was detected."""
        capture_ns = self._resolve_capture_mono_ns(capture_mono_ns)
        if self._face_detected_prev:
            self._face_lost_frames += 1

            if self._last_face_seen_mono_ns is None:
                interval_ns = max(
                    1, int(1_000_000_000 / max(1, self._config.fps))
                )
                self._last_face_seen_mono_ns = capture_ns - interval_ns

            tolerance_seconds = self._face_lost_tolerance_seconds()
            elapsed_seconds = (
                (capture_ns - self._last_face_seen_mono_ns) / 1_000_000_000.0
                if self._last_face_seen_mono_ns is not None
                else float("inf")
            )
            # Hysteresis is elapsed-time based. Frame count remains diagnostic
            # only and cannot lengthen tolerance under low/variable FPS.
            if elapsed_seconds <= tolerance_seconds:
                return FaceTrackingResult(
                    face_detected=False,
                    confidence=0.0,
                    landmarks=None,
                    landmarks_px=None,
                    bounding_box=None,
                    face_stable=True,  # Still within tolerance
                    observed_at_mono_ns=capture_ns,
                    detector_timestamp_ms=detector_timestamp_ms,
                    detector_timestamp_adjusted=detector_timestamp_adjusted,
                )

            # Tolerance exceeded — face truly lost
            self._face_detected_prev = False
            self._face_stable = False
            self._prev_landmarks_px = None
            self._prev_landmarks_mono_ns = None
            self._prev_face_width_px = None

        return FaceTrackingResult(
            face_detected=False,
            confidence=0.0,
            landmarks=None,
            landmarks_px=None,
            bounding_box=None,
            face_stable=False,
            observed_at_mono_ns=capture_ns,
            detector_timestamp_ms=detector_timestamp_ms,
            detector_timestamp_adjusted=detector_timestamp_adjusted,
        )

    def _resolve_capture_mono_ns(self, supplied: int | None) -> int:
        """Resolve explicit capture time or a deterministic legacy cadence."""

        if supplied is not None:
            if supplied < 0:
                raise ValueError("capture_mono_ns must be non-negative")
            self._synthetic_capture_mono_ns = max(
                self._synthetic_capture_mono_ns, supplied
            )
            return supplied
        interval_ns = max(1, int(1_000_000_000 / max(1, self._config.fps)))
        self._synthetic_capture_mono_ns += interval_ns
        return self._synthetic_capture_mono_ns

    def _face_lost_tolerance_seconds(self) -> float:
        explicit = self._config.face_lost_tolerance_seconds
        if explicit is not None:
            return explicit
        return self._config.face_lost_tolerance_frames / max(1.0, float(self._config.fps))

    def _compute_confidence(self, landmarks: NDArray[np.float32]) -> float:
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

    def compute_nose_tip_displacement(
        self, current_landmarks_px: NDArray[np.float32]
    ) -> float:
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
        self, landmarks: NDArray[np.float32], indices: list[int]
    ) -> NDArray[np.float32]:
        """
        Extract a subset of landmarks by index.

        Args:
            landmarks: Full (N, 3) or (N, 2) landmark array
            indices: List of landmark indices to extract

        Returns:
            Subset array of shape (len(indices), D)
        """
        return landmarks[indices]
