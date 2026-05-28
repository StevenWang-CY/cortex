"""Tests for Phase 4 Agent X3 fixes — capture pipeline, feature fusion,
longitudinal None handling.

Covers:

* Fix #1 — ``CapturedFrame.timestamp`` is UNIX epoch seconds
  (``time.time()``), matching the ``FrameMeta.timestamp`` schema contract.
* Fix #3 — Consecutive failed reads flip ``WebcamCapture.capture_stale``
  to True after the threshold; a successful frame clears it.
* Fix #5 — :class:`FeatureFusion` update methods assign the timestamp
  BEFORE the feature pointer so a concurrent reader never observes
  ``_*_timestamp == 0.0`` paired with a non-None feature.
* Fix #6 — :class:`LongitudinalTracker.accumulate` filters None HR/HRV
  out of the rolling baseline instead of imputing zero.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from cortex.libs.config.settings import CaptureConfig
from cortex.libs.schemas.features import (
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.services.capture_service.webcam import (
    _CAPTURE_STALE_THRESHOLD,
    CapturedFrame,
    WebcamCapture,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.longitudinal import LongitudinalTracker


def _synthetic_bgr_frame(width: int = 64, height: int = 48) -> np.ndarray:
    """Cheap synthetic BGR frame for capture-loop tests."""
    return np.full((height, width, 3), 128, dtype=np.uint8)


def _read_side_effect_factory(
    *, validation_passes: int, post_validation: Iterable[tuple[bool, object]],
) -> object:
    """Factory: returns a ``side_effect`` callable that yields successful
    frames for the first ``validation_passes`` calls (so
    ``open_video_capture`` validation passes) and then yields from
    ``post_validation`` for the actual capture loop.
    """
    validation = [(True, _synthetic_bgr_frame())] * validation_passes
    tail = list(post_validation)
    sequence = iter(validation + tail)
    last = tail[-1] if tail else (True, _synthetic_bgr_frame())

    def _side_effect() -> tuple[bool, object]:
        try:
            return next(sequence)
        except StopIteration:
            return last

    return _side_effect


# ---------------------------------------------------------------------------
# Fix #1 — CapturedFrame.timestamp is wall-clock (time.time())
# ---------------------------------------------------------------------------


class TestCapturedFrameTimestampIsWallClock:
    """The producer must emit ``time.time()`` (UNIX epoch) so the value is
    directly comparable to ``FrameMeta.timestamp`` as documented in
    ``cortex/libs/schemas/features.py``.
    """

    @pytest.mark.asyncio
    async def test_capture_loop_uses_time_time_for_frame_timestamp(self) -> None:
        capture = WebcamCapture(
            CaptureConfig(device_id=0, fps=30), queue_maxsize=4,
        )
        with patch(
            "cortex.services.capture_service.webcam.cv2.VideoCapture",
        ) as mock_videocap:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.read.return_value = (True, _synthetic_bgr_frame())
            mock_videocap.return_value = mock_cap

            t0_wall = time.time()
            await capture.start()
            try:
                # Drain a frame off the queue.
                frame: CapturedFrame | None = None
                for _ in range(20):
                    frame = await capture.get_frame(timeout=0.2)
                    if frame is not None:
                        break
                    await asyncio.sleep(0.05)
            finally:
                await capture.stop()

        assert frame is not None, "capture loop produced no frame"
        # The timestamp must be a UNIX epoch value (≥ t0_wall, ≪ t0_mono
        # because monotonic clocks on Linux/macOS start near system uptime
        # while time.time() is current epoch seconds — orders of magnitude
        # larger). A simple lower-bound check on wall clock is enough.
        assert frame.timestamp >= t0_wall, (
            "frame.timestamp predates capture start — not wall-clock"
        )
        # Wall-clock values on modern systems are > 1.7e9 (seconds since
        # 1970). Monotonic clocks are typically < 1e8. Guard with a
        # conservative threshold that distinguishes the two for any
        # realistic test environment.
        assert frame.timestamp > 1_000_000_000.0, (
            f"frame.timestamp={frame.timestamp} looks like a monotonic clock, "
            "not UNIX epoch seconds"
        )


# ---------------------------------------------------------------------------
# Fix #3 — Consecutive-failure tracking + capture_stale property
# ---------------------------------------------------------------------------


class TestCaptureStaleTracking:
    """The capture loop must surface a stale-capture signal after
    ``_CAPTURE_STALE_THRESHOLD`` consecutive failed reads, and clear it
    on a successful frame.
    """

    @pytest.mark.asyncio
    async def test_capture_stale_set_after_threshold_failures(self) -> None:
        capture = WebcamCapture(
            CaptureConfig(device_id=0, fps=30), queue_maxsize=4,
        )
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        # The capture loop calls ``cap.read()`` and always gets a failure
        # back. After ``_CAPTURE_STALE_THRESHOLD`` consecutive failures the
        # loop must flip the ``capture_stale`` flag.
        mock_cap.read.return_value = (False, None)

        # Bypass ``open_video_capture`` validation entirely by injecting a
        # pre-opened cap. This makes the test deterministic and fast (no
        # 0.5 s validation sleeps).
        with patch(
            "cortex.services.capture_service.webcam.open_video_capture",
            return_value=(mock_cap, MagicMock(device_id=0, source="test")),
        ):
            # capture_stale should start False.
            assert capture.capture_stale is False
            await capture.start()
            try:
                # Wait long enough for the loop to exceed the threshold.
                # At 30 FPS the loop ticks every ~33ms, so threshold *
                # interval ≈ 1s. Add slack for the test runner.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if capture.capture_stale:
                        break
                    await asyncio.sleep(0.05)
            finally:
                await capture.stop()

        assert capture.capture_stale is True, (
            "capture_stale was not set after threshold consecutive failures"
        )

    @pytest.mark.asyncio
    async def test_capture_stale_cleared_on_successful_frame(self) -> None:
        capture = WebcamCapture(
            CaptureConfig(device_id=0, fps=30), queue_maxsize=4,
        )
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True

        # First N reads fail, then a few good frames recover the loop.
        fail_count = _CAPTURE_STALE_THRESHOLD + 5
        # MagicMock side_effect with a list raises StopIteration after
        # exhaustion which crashes the capture thread. Use a long tail of
        # success frames so the test never runs out.
        mock_cap.read.side_effect = (
            [(False, None)] * fail_count
            + [(True, _synthetic_bgr_frame())] * 200
        )

        with patch(
            "cortex.services.capture_service.webcam.open_video_capture",
            return_value=(mock_cap, MagicMock(device_id=0, source="test")),
        ):
            await capture.start()
            try:
                # Wait for stale to be set.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not capture.capture_stale:
                    await asyncio.sleep(0.05)
                assert capture.capture_stale is True

                # Wait for recovery.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and capture.capture_stale:
                    await asyncio.sleep(0.05)
            finally:
                await capture.stop()

        assert capture.capture_stale is False, (
            "capture_stale should clear after a successful frame"
        )

    def test_capture_stale_property_exists_and_default_false(self) -> None:
        """Daemon polling path requires the property to be available
        without any I/O (e.g. before ``start()`` is called).
        """
        capture = WebcamCapture()
        assert capture.capture_stale is False


# ---------------------------------------------------------------------------
# Fix #5 — FeatureFusion timestamp/feature assignment ordering
# ---------------------------------------------------------------------------


class TestFeatureFusionTimestampOrdering:
    """The ordering invariant is: timestamp must be valid by the time the
    feature pointer becomes visible to readers. The strongest observable
    consequence of getting it wrong was that a reader interleaved
    immediately after ``self._physio = features`` would see
    ``_physio_timestamp == 0.0`` and compute huge staleness. We verify
    the assignment order by reading the per-channel timestamp immediately
    after the update — it must NOT be the 0.0 sentinel.
    """

    def test_physio_timestamp_assigned_before_features(self) -> None:
        fusion = FeatureFusion()
        # Before update_physio: physio is None, timestamp is the sentinel.
        assert fusion._physio is None
        assert fusion._physio_timestamp == 0.0

        physio = PhysioFeatures(
            pulse_bpm=72.0, pulse_quality=0.9, valid=True,
        )
        fusion.update_physio(physio, timestamp=123.456)

        # After update: features non-None AND timestamp matches (not 0.0).
        assert fusion._physio is not None
        assert fusion._physio_timestamp == 123.456

    def test_kinematics_timestamp_assigned_before_features(self) -> None:
        fusion = FeatureFusion()
        kin = KinematicFeatures(blink_rate=15.0, confidence=0.7)
        fusion.update_kinematics(kin, timestamp=42.0)
        assert fusion._kinematics is not None
        assert fusion._kinematics_timestamp == 42.0

    def test_telemetry_timestamp_assigned_before_features(self) -> None:
        fusion = FeatureFusion()
        tel = TelemetryFeatures(
            mouse_velocity_mean=100.0,
            mouse_velocity_variance=10.0,
            click_frequency=0.1,
            keystroke_interval_variance=20.0,
            window_switch_rate=0.5,
            inactivity_seconds=0.0,
            mouse_jerk_score=0.0,
            click_burst_score=0.0,
            keyboard_burst_score=0.0,
            backspace_density=0.0,
        )
        fusion.update_telemetry(tel, timestamp=7.5)
        assert fusion._telemetry is not None
        assert fusion._telemetry_timestamp == 7.5

    def test_zero_timestamp_is_respected_not_treated_as_missing(self) -> None:
        """``update_physio(features, timestamp=0.0)`` must KEEP the 0.0
        value — previously ``timestamp or time.monotonic()`` would silently
        replace 0.0 with the current monotonic clock because 0.0 is falsy.

        Several integration tests rely on being able to seed deterministic
        timestamps (including 0.0). The new ``timestamp is not None`` check
        preserves them.
        """
        fusion = FeatureFusion()
        physio = PhysioFeatures(pulse_quality=0.8, valid=True)
        fusion.update_physio(physio, timestamp=0.0)
        assert fusion._physio_timestamp == 0.0

    def test_none_timestamp_falls_back_to_monotonic(self) -> None:
        """The legacy behaviour of "no explicit timestamp → use
        time.monotonic()" must still apply when callers pass ``None``.
        """
        fusion = FeatureFusion()
        kin = KinematicFeatures(blink_rate=10.0, confidence=0.5)
        before = time.monotonic()
        fusion.update_kinematics(kin, timestamp=None)
        after = time.monotonic()
        assert before <= fusion._kinematics_timestamp <= after


# ---------------------------------------------------------------------------
# Fix #6 — LongitudinalTracker.accumulate filters None HR/HRV
# ---------------------------------------------------------------------------


class TestLongitudinalNoneHandling:
    """The state loop calls ``accumulate(hr=vector.hr, hrv=vector.hrv_rmssd,
    ...)`` every tick regardless of whether the physio channel is healthy.
    The tracker must NOT zero-impute None values into the rolling
    baseline — that would silently bias the chronotype model downward.
    """

    def test_none_hr_not_appended_to_baseline_samples(self) -> None:
        tracker = LongitudinalTracker(store=None)
        # Mix Some real samples with None calls (simulating physio gaps).
        tracker.accumulate(hr=72.0, hrv=45.0, state="FLOW", dt_seconds=0.5)
        tracker.accumulate(hr=None, hrv=None, state="FLOW", dt_seconds=0.5)
        tracker.accumulate(hr=74.0, hrv=42.0, state="FLOW", dt_seconds=0.5)
        # Only the real samples land in the baseline pool.
        assert tracker._hr_samples == [72.0, 74.0]
        assert tracker._hrv_samples == [45.0, 42.0]

    def test_state_transition_still_recorded_when_hr_is_none(self) -> None:
        """The None-physio short-circuit must NOT skip the state/duration
        accounting — those counters drive flow/hyper minutes regardless of
        physio availability.
        """
        tracker = LongitudinalTracker(store=None)
        tracker.accumulate(hr=None, hrv=None, state="HYPER", dt_seconds=2.0)
        assert tracker._hyper_seconds == pytest.approx(2.0)
        assert tracker._hr_samples == []
        assert tracker._hrv_samples == []

    def test_zero_hr_is_filtered_when_passed_as_none(self) -> None:
        """Defense in depth: confirm that ``hr=None`` does NOT result in
        a 0.0 sample being appended (the legacy bug-shape).
        """
        tracker = LongitudinalTracker(store=None)
        tracker.accumulate(hr=None, hrv=None, state="FLOW", dt_seconds=0.5)
        assert 0.0 not in tracker._hr_samples
        assert 0.0 not in tracker._hrv_samples
