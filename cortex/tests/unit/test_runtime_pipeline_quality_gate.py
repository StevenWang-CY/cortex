"""P0-2: low-quality frames must be excluded from the rPPG RGB window.

The guard was added inside ``CortexDaemon._process_capture_output``:
if ``output.frame_meta.low_quality`` is True the combined_rgb sample is
NOT appended to ``_rgb_history``, and ``_frames_low_quality_rejected``
is incremented instead.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _make_frame_meta(low_quality: bool = False, timestamp: float = 0.0) -> object:
    """Return a minimal FrameMeta-like namespace."""
    return SimpleNamespace(
        timestamp=timestamp,
        face_detected=True,
        face_confidence=0.95,
        brightness_score=0.8,
        blur_score=0.9,
        motion_score=0.8,
        low_quality=low_quality,
    )


def _make_landmarks() -> object:
    """Return a non-None landmarks placeholder (shape not validated in test)."""
    return np.zeros((468, 2), dtype=np.float32)


def _make_pipeline_output(low_quality: bool = False, ts: float = 0.0) -> object:
    """Return a minimal PipelineOutput-like namespace."""
    return SimpleNamespace(
        frame_meta=_make_frame_meta(low_quality=low_quality, timestamp=ts),
        landmarks_px=_make_landmarks(),
        frame=np.zeros((480, 640, 3), dtype=np.uint8),
    )


@pytest.mark.asyncio
async def test_low_quality_frames_excluded_from_rgb_history() -> None:
    """Two ok + one low-quality → _rgb_history has 2, counter has 1."""
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    cfg = get_config()
    daemon = CortexDaemon(config=cfg)

    # Pre-wire a counter (should already exist after our fix)
    assert hasattr(daemon, "_frames_low_quality_rejected")
    initial_count = daemon._frames_low_quality_rejected
    initial_history_len = len(daemon._rgb_history)

    # Build fake RGB array returned by roi_frame.combined_rgb()
    fake_rgb = np.ones(3, dtype=np.float64)

    # Fake roi_frame that always returns a non-None combined_rgb
    fake_roi = MagicMock()
    fake_roi.combined_rgb.return_value = fake_rgb
    fake_roi.head_jitter_px = 0.0

    outputs = [
        _make_pipeline_output(low_quality=False, ts=1.0),
        _make_pipeline_output(low_quality=True, ts=2.0),   # should be rejected
        _make_pipeline_output(low_quality=False, ts=3.0),
    ]

    # Build stub return values for kinematics sub-detectors
    fake_blink = SimpleNamespace(
        blink_rate=None,
        blink_rate_delta=None,
        blink_suppression_score=None,
        perclos_60s=None,
        mean_blink_duration_ms=None,
        ear_variance=None,
    )
    fake_pose = SimpleNamespace(pitch=None, yaw=None, roll=None)
    fake_posture = SimpleNamespace(
        slump_score=None,
        forward_lean_score=None,
        shoulder_drop_ratio=None,
    )

    # Patch the roi_extractor and the downstream calls we don't want
    with (
        patch.object(daemon, "_roi_extractor") as mock_roi_extractor,
        patch.object(daemon, "_blink_detector") as mock_blink,
        patch.object(daemon, "_head_pose") as mock_pose,
        patch.object(daemon, "_posture") as mock_posture,
        patch.object(daemon, "_feature_fusion") as mock_fusion,
    ):
        mock_roi_extractor.extract.return_value = fake_roi
        mock_blink.update.return_value = fake_blink
        mock_pose.update.return_value = fake_pose
        mock_posture.update_with_face.return_value = fake_posture
        mock_fusion.update_kinematics.return_value = None
        mock_fusion.update_physio.return_value = None

        # We also need to stub the registry call
        with patch("cortex.services.runtime_daemon.registry") as mock_reg:
            mock_reg.register.return_value = None
            for out in outputs:
                await daemon._process_capture_output(out)

    rgb_added = len(daemon._rgb_history) - initial_history_len
    rejected = daemon._frames_low_quality_rejected - initial_count

    assert rgb_added == 2, f"expected 2 rgb samples, got {rgb_added}"
    assert rejected == 1, f"expected 1 low-quality rejection, got {rejected}"
