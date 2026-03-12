"""
Capture Service — Frame Quality Scoring

Assesses frame quality for downstream processing via three metrics:
- Brightness: mean pixel intensity, flags frames below ~50 lux equivalent
- Blur: Laplacian variance, detects out-of-focus or motion-blurred frames
- Motion: inter-frame landmark jitter at nose tip, discards excessive motion

Produces a composite quality gate that determines if a frame should be
forwarded to the physio/kinematics pipeline.

Design:
- Each metric produces a 0-1 score (1.0 = best quality)
- Composite gate requires all three metrics above configurable thresholds
- Thresholds are lenient to avoid excessive frame dropping
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from cortex.libs.config.settings import CaptureConfig

logger = logging.getLogger(__name__)

# Empirical thresholds
_BRIGHTNESS_LOW = 50  # Mean pixel intensity below this is "too dark"
_BRIGHTNESS_HIGH = 220  # Mean pixel intensity above this is "too bright"
_BLUR_VARIANCE_LOW = 20.0  # Laplacian variance below this is "too blurry"
_BLUR_VARIANCE_HIGH = 500.0  # Normalization ceiling for blur score


@dataclass(frozen=True)
class FrameQuality:
    """Quality assessment for a single frame."""

    brightness_score: float  # 0.0 (too dark/bright) to 1.0 (ideal)
    blur_score: float  # 0.0 (very blurry) to 1.0 (sharp)
    motion_score: float  # 0.0 (excessive jitter) to 1.0 (stable)
    passed: bool  # Whether frame passes the composite quality gate


class FrameQualityScorer:
    """
    Scores frame quality along three axes: brightness, blur, and motion.

    Usage:
        scorer = FrameQualityScorer(config)
        quality = scorer.score(frame, nose_displacement=2.5)
    """

    def __init__(self, config: CaptureConfig | None = None) -> None:
        self._config = config or CaptureConfig()

        # Quality gate thresholds (frames must exceed these to pass)
        self._brightness_threshold = 0.2
        self._blur_threshold = 0.15
        self._motion_threshold = 0.3

    def score(
        self,
        frame: np.ndarray,
        nose_displacement: float = 0.0,
    ) -> FrameQuality:
        """
        Score a frame's quality.

        Args:
            frame: BGR uint8 image, shape (H, W, 3)
            nose_displacement: Inter-frame nose tip displacement in pixels.
                If 0.0, motion_score defaults to 1.0 (first frame or no tracking).

        Returns:
            FrameQuality with per-axis scores and gate decision.
        """
        brightness = self._score_brightness(frame)
        blur = self._score_blur(frame)
        motion = self._score_motion(nose_displacement)

        passed = (
            brightness >= self._brightness_threshold
            and blur >= self._blur_threshold
            and motion >= self._motion_threshold
        )

        return FrameQuality(
            brightness_score=brightness,
            blur_score=blur,
            motion_score=motion,
            passed=passed,
        )

    def _score_brightness(self, frame: np.ndarray) -> float:
        """
        Score frame brightness.

        Uses mean pixel intensity of the grayscale image.
        Flags frames below ~50 lux (mapped to pixel intensity ~50/255).

        Args:
            frame: BGR uint8 image

        Returns:
            Score 0.0 to 1.0 (1.0 = ideal brightness)
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_intensity = float(np.mean(gray))

        if mean_intensity < _BRIGHTNESS_LOW:
            # Too dark: linear ramp from 0 at intensity=0 to threshold score
            return mean_intensity / _BRIGHTNESS_LOW * 0.5
        elif mean_intensity > _BRIGHTNESS_HIGH:
            # Too bright: linear ramp down
            return max(0.0, 1.0 - (mean_intensity - _BRIGHTNESS_HIGH) / (255 - _BRIGHTNESS_HIGH))
        else:
            # Good range: map [_LOW, _HIGH] to [0.5, 1.0]
            # Peak at ~128 (middle)
            distance_from_ideal = abs(mean_intensity - 128) / 128
            return 1.0 - 0.3 * distance_from_ideal

    def _score_blur(self, frame: np.ndarray) -> float:
        """
        Score frame sharpness using Laplacian variance.

        Higher variance = sharper image = better quality.

        Args:
            frame: BGR uint8 image

        Returns:
            Score 0.0 to 1.0 (1.0 = sharp)
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        variance = float(laplacian.var())

        if variance < _BLUR_VARIANCE_LOW:
            # Very blurry
            return variance / _BLUR_VARIANCE_LOW * 0.3
        elif variance >= _BLUR_VARIANCE_HIGH:
            return 1.0
        else:
            # Map [LOW, HIGH] to [0.3, 1.0]
            normalized = (variance - _BLUR_VARIANCE_LOW) / (
                _BLUR_VARIANCE_HIGH - _BLUR_VARIANCE_LOW
            )
            return 0.3 + 0.7 * normalized

    def _score_motion(self, nose_displacement: float) -> float:
        """
        Score inter-frame motion stability.

        Uses nose tip displacement from face tracker. Frames with displacement
        exceeding max_jitter_px (default 5.0) are flagged as too jittery.

        Args:
            nose_displacement: Pixel displacement of nose tip between frames.

        Returns:
            Score 0.0 to 1.0 (1.0 = stable)
        """
        if nose_displacement <= 0.0:
            # First frame or no tracking data — assume stable
            return 1.0

        max_jitter = self._config.max_jitter_px

        if nose_displacement <= max_jitter * 0.5:
            # Very stable
            return 1.0
        elif nose_displacement >= max_jitter * 2:
            # Excessive motion
            return 0.0
        else:
            # Linear interpolation between stable and excessive
            # [0.5 * max, 2.0 * max] -> [1.0, 0.0]
            range_start = max_jitter * 0.5
            range_end = max_jitter * 2.0
            return 1.0 - (nose_displacement - range_start) / (range_end - range_start)
