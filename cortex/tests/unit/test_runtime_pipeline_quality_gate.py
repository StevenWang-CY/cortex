"""WP-2: missing/low-quality camera observations retain window exposure.

The behaviour lives inside ``CortexDaemon._process_capture_output``: a
low-quality frame is NOT contributed as a real RGB sample, but a NaN
observation IS appended so the bounded time-series still advances by one
slot. ``_frames_low_quality_rejected`` is incremented. Interpolation is
permitted only after the coverage/gap gate accepts the window.
"""

from __future__ import annotations

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


def _stub_kinematics() -> tuple[object, object, object]:
    fake_blink = SimpleNamespace(
        blink_rate=None,
        blink_rate_delta=None,
        blink_suppression_score=None,
        perclos_60s=None,
        mean_blink_duration_ms=None,
        ear_variance=None,
        valid_exposure_seconds=0.0,
    )
    fake_pose = SimpleNamespace(
        pitch=0.0,
        yaw=0.0,
        roll=0.0,
        angular_velocity_deg_per_s=0.0,
        is_jittery=False,
        is_frozen=False,
    )
    fake_posture = SimpleNamespace(
        head_neck_flexion_angle=None,
        head_neck_flexion_score=None,
        sustained_flexion_seconds=0.0,
        proxy_available=False,
    )
    return fake_blink, fake_pose, fake_posture


async def _run_outputs(daemon: object, outputs: list[object]) -> None:
    fake_rgb = np.ones(3, dtype=np.float64)
    fake_roi = MagicMock()
    fake_roi.combined_rgb.return_value = fake_rgb
    fake_roi.head_jitter_px = 0.0
    fake_blink, fake_pose, fake_posture = _stub_kinematics()
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
        mock_posture.face_scale.return_value = 100.0
        mock_posture.update.return_value = fake_posture
        mock_fusion.update_kinematics.return_value = None
        mock_fusion.update_physio.return_value = None
        with patch("cortex.services.runtime_daemon.registry") as mock_reg:
            mock_reg.register.return_value = None
            for out in outputs:
                await daemon._process_capture_output(out)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_low_quality_frames_nan_filled_keeps_window_time_uniform() -> None:
    """audit P1: a low-quality frame is appended as a NaN sentinel (not
    dropped), so all frames advance the window and the counter still ticks.

    Two ok + one low-quality → _rgb_history grows by 3 (one row is NaN),
    counter increments by 1, and the window stays time-uniform (every
    frame occupies exactly one slot)."""
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    cfg = get_config()
    daemon = CortexDaemon(config=cfg)

    assert hasattr(daemon, "_frames_low_quality_rejected")
    initial_count = daemon._frames_low_quality_rejected
    initial_history_len = len(daemon._rgb_history)

    outputs = [
        _make_pipeline_output(low_quality=False, ts=1.0),
        _make_pipeline_output(low_quality=True, ts=2.0),   # NaN-filled, not dropped
        _make_pipeline_output(low_quality=False, ts=3.0),
    ]

    await _run_outputs(daemon, outputs)

    rgb_added = len(daemon._rgb_history) - initial_history_len
    rejected = daemon._frames_low_quality_rejected - initial_count

    # audit P1: ALL three frames occupy a slot — the window is time-uniform.
    assert rgb_added == 3, f"expected 3 window slots, got {rgb_added}"
    assert rejected == 1, f"expected 1 low-quality rejection, got {rejected}"

    # Exactly one of the appended rows is the NaN sentinel.
    appended = list(daemon._rgb_history)[-3:]
    nan_rows = sum(1 for row in appended if np.isnan(np.asarray(row)).all())
    assert nan_rows == 1, f"expected exactly one NaN sentinel row, got {nan_rows}"


def test_interpolate_nan_window_fills_gaps_finite() -> None:
    """The NaN-interpolation helper must leave NO NaNs and bridge gaps
    linearly so a non-NaN-aware filter downstream never sees a NaN."""
    from cortex.services.runtime_daemon import _interpolate_nan_window

    # frame 1 is a NaN-sentinel between two finite frames.
    window = np.array(
        [[1.0, 2.0, 3.0], [np.nan, np.nan, np.nan], [3.0, 4.0, 5.0]],
        dtype=np.float64,
    )
    out = _interpolate_nan_window(window)
    assert not np.isnan(out).any(), "interpolated window still contains NaN"
    # Linear interpolation of the middle row between 1.0 and 3.0 → 2.0, etc.
    np.testing.assert_allclose(out[1], [2.0, 3.0, 4.0])
    # Endpoints untouched.
    np.testing.assert_allclose(out[0], [1.0, 2.0, 3.0])
    np.testing.assert_allclose(out[2], [3.0, 4.0, 5.0])


def test_interpolate_nan_window_all_nan_channel_stays_unavailable() -> None:
    """An all-missing channel must never be fabricated into a zero signal."""
    from cortex.services.runtime_daemon import _interpolate_nan_window

    window = np.full((4, 3), np.nan, dtype=np.float64)
    out = _interpolate_nan_window(window)
    assert np.isnan(out).all()
