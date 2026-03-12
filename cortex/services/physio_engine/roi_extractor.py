"""
Physio Engine — ROI RGB Trace Extraction

Extracts mean RGB color values from face regions of interest (ROIs) defined
by MediaPipe FaceMesh landmark indices. These traces form the raw input to
the rPPG algorithms (POS, CHROM, green-channel).

ROI regions (from spec):
- Forehead: landmarks 10, 67, 69, 104, 108, 151, 299, 337, 338
- Left cheek: landmarks 50, 101, 116–121
- Right cheek: mirrored landmarks 280, 330, 345–350

Design:
- Convex hull from landmark pixel coordinates defines the ROI polygon
- Spatial averaging of pixels within the polygon produces one (R, G, B) per ROI
- Dynamic ROI selection picks the region with highest SNR
- No frame storage — ephemeral processing only
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import LandmarksConfig, get_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoiTrace:
    """Mean RGB values from a single ROI region."""

    r: float
    g: float
    b: float
    pixel_count: int  # number of pixels in the ROI (for weighting)

    def to_array(self) -> NDArray[np.float64]:
        """Return [R, G, B] as numpy array."""
        return np.array([self.r, self.g, self.b], dtype=np.float64)


@dataclass(frozen=True)
class RoiTraceFrame:
    """RGB traces from all ROI regions for a single frame."""

    forehead: RoiTrace | None
    left_cheek: RoiTrace | None
    right_cheek: RoiTrace | None
    timestamp: float

    @property
    def best_roi(self) -> RoiTrace | None:
        """Return the ROI with the most pixels (best spatial coverage)."""
        candidates = [
            roi for roi in [self.forehead, self.left_cheek, self.right_cheek]
            if roi is not None and roi.pixel_count > 0
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.pixel_count)

    @property
    def has_any_roi(self) -> bool:
        """Check if any ROI was successfully extracted."""
        return any(
            roi is not None and roi.pixel_count > 0
            for roi in [self.forehead, self.left_cheek, self.right_cheek]
        )

    def combined_rgb(self) -> NDArray[np.float64] | None:
        """
        Pixel-count-weighted average of all available ROIs.

        Returns:
            Array of shape (3,) with [R, G, B] means, or None if no ROIs.
        """
        rois = [
            roi for roi in [self.forehead, self.left_cheek, self.right_cheek]
            if roi is not None and roi.pixel_count > 0
        ]
        if not rois:
            return None

        total_pixels = sum(r.pixel_count for r in rois)
        weighted_sum = np.zeros(3, dtype=np.float64)
        for roi in rois:
            weighted_sum += roi.to_array() * roi.pixel_count
        return weighted_sum / total_pixels


class RoiExtractor:
    """
    Extracts mean RGB traces from face ROI regions.

    Uses landmark pixel coordinates to define convex hull polygons for each
    ROI, then computes spatial mean of pixel values within each polygon.

    Usage:
        extractor = RoiExtractor()
        trace = extractor.extract(frame, landmarks_px, timestamp)
    """

    def __init__(self, landmarks_config: LandmarksConfig | None = None) -> None:
        self._config = landmarks_config or get_config().landmarks

    def extract(
        self,
        frame: NDArray[np.uint8],
        landmarks_px: NDArray[np.float32],
        timestamp: float,
    ) -> RoiTraceFrame:
        """
        Extract RGB traces from all ROI regions.

        Args:
            frame: BGR uint8 image, shape (H, W, 3)
            landmarks_px: Pixel coordinates, shape (N, 2)
            timestamp: Frame timestamp (monotonic seconds)

        Returns:
            RoiTraceFrame with traces from each region.
        """
        h, w = frame.shape[:2]

        forehead = self._extract_roi(
            frame, landmarks_px, self._config.forehead, h, w
        )
        left_cheek = self._extract_roi(
            frame, landmarks_px, self._config.left_cheek, h, w
        )
        right_cheek = self._extract_roi(
            frame, landmarks_px, self._config.right_cheek, h, w
        )

        return RoiTraceFrame(
            forehead=forehead,
            left_cheek=left_cheek,
            right_cheek=right_cheek,
            timestamp=timestamp,
        )

    def _extract_roi(
        self,
        frame: NDArray[np.uint8],
        landmarks_px: NDArray[np.float32],
        indices: list[int],
        height: int,
        width: int,
    ) -> RoiTrace | None:
        """
        Extract mean RGB from a single ROI defined by landmark indices.

        Creates a convex hull from the landmark points and computes the
        spatial mean of all pixels inside the hull.

        Args:
            frame: BGR image
            landmarks_px: All face landmarks in pixel coords (N, 2)
            indices: Landmark indices defining this ROI
            height: Frame height
            width: Frame width

        Returns:
            RoiTrace with mean RGB, or None if extraction fails.
        """
        # Validate indices are within landmark array bounds
        max_idx = landmarks_px.shape[0]
        valid_indices = [i for i in indices if 0 <= i < max_idx]
        if len(valid_indices) < 3:
            return None

        # Get ROI landmark points
        points = landmarks_px[valid_indices].astype(np.int32)

        # Clip to frame boundaries
        points[:, 0] = np.clip(points[:, 0], 0, width - 1)
        points[:, 1] = np.clip(points[:, 1], 0, height - 1)

        # Compute convex hull
        try:
            hull = cv2.convexHull(points)
        except cv2.error:
            return None

        if hull is None or len(hull) < 3:
            return None

        # Create binary mask from convex hull
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillConvexPoly(mask, hull, 1)

        # Count pixels in ROI
        pixel_count = int(np.sum(mask))
        if pixel_count == 0:
            return None

        # Extract mean RGB (frame is BGR, convert to RGB order)
        # Use mask to select only ROI pixels
        roi_pixels = frame[mask == 1]  # shape (pixel_count, 3) in BGR
        mean_bgr = np.mean(roi_pixels, axis=0)

        return RoiTrace(
            r=float(mean_bgr[2]),  # BGR -> R
            g=float(mean_bgr[1]),  # BGR -> G
            b=float(mean_bgr[0]),  # BGR -> B
            pixel_count=pixel_count,
        )

    def extract_single_roi(
        self,
        frame: NDArray[np.uint8],
        landmarks_px: NDArray[np.float32],
        roi_name: str,
    ) -> RoiTrace | None:
        """
        Extract RGB from a single named ROI.

        Args:
            frame: BGR image
            landmarks_px: Face landmarks in pixel coords
            roi_name: One of 'forehead', 'left_cheek', 'right_cheek'

        Returns:
            RoiTrace or None.
        """
        h, w = frame.shape[:2]
        indices_map = {
            "forehead": self._config.forehead,
            "left_cheek": self._config.left_cheek,
            "right_cheek": self._config.right_cheek,
        }
        indices = indices_map.get(roi_name)
        if indices is None:
            raise ValueError(f"Unknown ROI: {roi_name}. Valid: {list(indices_map.keys())}")
        return self._extract_roi(frame, landmarks_px, indices, h, w)
