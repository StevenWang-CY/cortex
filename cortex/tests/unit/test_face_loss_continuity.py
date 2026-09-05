"""Audit defect D6: a brief face loss must not discard camera-derived state.

The tracker's frame-count hysteresis (5 frames = 167 ms at 30 fps) used to
reset the RGB history, the beat ledger, blink exposure and head pose.  The
``FaceLossTracker`` policy issues a reset only once a loss exceeds the physio
interpolation gap, and the time-indexed consumers below are shown to survive
a 200 ms NO_FACE dropout intact.
"""

from __future__ import annotations

from uuid import UUID

import numpy as np
import pytest

from cortex.libs.config.settings import BlinkSignalConfig, LandmarksConfig
from cortex.libs.schemas.observations import MissingReason, ObservationValidity
from cortex.libs.schemas.physiology import BeatStatus
from cortex.services.capture_service.continuity import (
    FaceLossTracker,
    should_reset_camera_state,
)
from cortex.services.capture_service.observation_buffer import (
    NumericObservation,
    ObservationBuffer,
    prepare_observation_window,
)
from cortex.services.kinematics_engine.blink_detector import BlinkDetector
from cortex.services.physio_engine.v2.backends import RPPGBackendRegistry
from cortex.services.physio_engine.v2.pulse import PulsePipelineV2

_BOOT = UUID("55555555-5555-5555-5555-555555555555")
FPS = 30.0
FRAME_NS = int(round(1e9 / FPS))
RESET_AFTER_MS = 250.0


def _eye(*, closed: bool) -> np.ndarray:
    vertical = 1.0 if closed else 11.0
    return np.asarray(
        [
            [100.0, 200.0],
            [115.0, 200.0 - vertical],
            [130.0, 200.0 - vertical],
            [145.0, 200.0],
            [130.0, 200.0 + vertical],
            [115.0, 200.0 + vertical],
        ],
        dtype=np.float64,
    )


def _face(*, closed: bool = False) -> np.ndarray:
    rng = np.random.default_rng(42)
    landmarks = np.empty((478, 2), dtype=np.float64)
    landmarks[:, 0] = 320.0 + rng.uniform(-80.0, 80.0, len(landmarks))
    landmarks[:, 1] = 240.0 + rng.uniform(-100.0, 100.0, len(landmarks))
    config = LandmarksConfig()
    eye = _eye(closed=closed)
    landmarks[config.left_eye] = eye
    landmarks[config.right_eye] = eye + np.asarray([180.0, 0.0])
    return landmarks


class TestFaceLossTracker:
    def test_policy_threshold(self) -> None:
        assert should_reset_camera_state(200.0, reset_after_ms=250.0) is False
        assert should_reset_camera_state(250.0, reset_after_ms=250.0) is False
        assert should_reset_camera_state(251.0, reset_after_ms=250.0) is True
        with pytest.raises(ValueError):
            should_reset_camera_state(1.0, reset_after_ms=-1.0)

    def test_short_dropout_transitions_without_reset(self) -> None:
        tracker = FaceLossTracker(reset_after_ms=RESET_AFTER_MS)
        decisions = []
        now = 1_000_000_000
        decisions.append(tracker.observe(face_present=True, observed_at_mono_ns=now))
        for _ in range(6):  # 6 missing frames = 200 ms since the last face
            now += FRAME_NS
            decisions.append(tracker.observe(face_present=False, observed_at_mono_ns=now))
        now += FRAME_NS
        reacquired = tracker.observe(face_present=True, observed_at_mono_ns=now)
        assert [item.transition for item in decisions[1:]] == ["lost", None, None, None, None, None]
        assert not any(item.should_reset for item in decisions)
        assert reacquired.transition == "reacquired"
        assert reacquired.should_reset is False
        # Measured from the last face-present frame: 7 intervals of 33.3 ms,
        # the same span the interpolation-gap gate sees between valid samples.
        assert reacquired.loss_duration_ms == pytest.approx(233.3, abs=1.0)
        assert decisions[-1].loss_duration_ms == pytest.approx(200.0, abs=1.0)
        assert tracker.face_present is True

    def test_long_dropout_resets_exactly_once(self) -> None:
        tracker = FaceLossTracker(reset_after_ms=RESET_AFTER_MS)
        now = 1_000_000_000
        tracker.observe(face_present=True, observed_at_mono_ns=now)
        resets = []
        for _ in range(12):  # 400 ms
            now += FRAME_NS
            resets.append(tracker.observe(face_present=False, observed_at_mono_ns=now).should_reset)
        now += FRAME_NS
        reacquired = tracker.observe(face_present=True, observed_at_mono_ns=now)
        assert resets.count(True) == 1
        # The 8th faceless frame is 266.7 ms after the last face: first > 250 ms.
        assert resets.index(True) == 7
        assert reacquired.should_reset is False
        # A second loss is a fresh decision.
        now += FRAME_NS
        again = tracker.observe(face_present=False, observed_at_mono_ns=now)
        assert again.transition == "lost"
        assert again.should_reset is False

    def test_reacquisition_after_threshold_without_intermediate_frames_resets_once(self) -> None:
        tracker = FaceLossTracker(reset_after_ms=RESET_AFTER_MS)
        tracker.observe(face_present=True, observed_at_mono_ns=1_000_000_000)
        tracker.observe(face_present=False, observed_at_mono_ns=1_033_000_000)
        reacquired = tracker.observe(face_present=True, observed_at_mono_ns=1_600_000_000)
        assert reacquired.transition == "reacquired"
        assert reacquired.should_reset is True

    def test_reset_forgets_the_loss(self) -> None:
        tracker = FaceLossTracker(reset_after_ms=RESET_AFTER_MS)
        tracker.observe(face_present=False, observed_at_mono_ns=1_000_000_000)
        tracker.reset()
        assert tracker.face_present is True
        assert tracker.loss_duration_ms(5_000_000_000) == 0.0


def _rgb_sample(index: int) -> np.ndarray:
    t = index / FPS
    pulse = np.sin(2 * np.pi * 1.2 * t)
    return np.asarray([100.0 + 0.4 * pulse, 90.0 + 1.5 * pulse, 80.0 + 0.2 * pulse])


def test_two_hundred_ms_no_face_dropout_retains_ledger_blink_exposure_and_rgb_history() -> None:
    tracker = FaceLossTracker(reset_after_ms=RESET_AFTER_MS)
    buffer: ObservationBuffer[NumericObservation] = ObservationBuffer(
        max_age_seconds=12.0, max_items=4000
    )
    pipeline = PulsePipelineV2(RPPGBackendRegistry.with_packaged_backends().resolve("pos"))
    blink = BlinkDetector(
        blink_config=BlinkSignalConfig(
            min_valid_exposure_seconds=1.0,
            history_window_seconds=60.0,
            max_valid_gap_ms=250.0,
        ),
        baseline_blink_rate=17.0,
    )
    dropout_start, dropout_frames = int(12.0 * FPS), 6  # 200 ms of NO_FACE
    origin_ns = 9_000_000_000
    last_update_ns: int | None = None
    accepted_before: set[str] = set()
    exposure_before = 0.0
    decisions = []
    last_result = None
    total_frames = int(20.0 * FPS)
    for index in range(total_frames):
        mono_ns = origin_ns + index * FRAME_NS
        seconds = mono_ns / 1e9
        missing = dropout_start <= index < dropout_start + dropout_frames
        decisions.append(tracker.observe(face_present=not missing, observed_at_mono_ns=mono_ns))
        if missing:
            blink.observe_missing(seconds)
        else:
            blink.update(_face(), seconds)
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=1_000 + index * 33,
                observed_at_mono_ns=mono_ns,
                boot_id=_BOOT,
                sequence=index,
                value=None if missing else _rgb_sample(index),
                validity=(
                    ObservationValidity.MISSING.value if missing else ObservationValidity.VALID.value
                ),
                missing_reason=MissingReason.NO_FACE if missing else None,
                quality=0.0 if missing else 0.9,
                motion_face_widths_per_second=None if missing else 0.05,
            )
        )
        if index == dropout_start - 1:
            accepted_before = {
                event.beat_id
                for event in pipeline.beat_events
                if event.status == BeatStatus.ACCEPTED.value
            }
            state = blink.latest_state
            assert state is not None
            exposure_before = state.valid_exposure_seconds
        if last_update_ns is None or mono_ns - last_update_ns >= 1_000_000_000:
            last_update_ns = mono_ns
            prepared = prepare_observation_window(
                buffer.snapshot(), window_seconds=10.0, nominal_fps=FPS
            )
            if prepared.ready:
                assert prepared.values is not None and prepared.sample_times_mono_ns is not None
                last_result = pipeline.process_window(
                    prepared.values,
                    prepared.sample_times_mono_ns,
                    sample_rate_hz=prepared.sample_rate_hz,
                    boot_id=_BOOT,
                    observation_quality=prepared.quality,
                    motion_face_widths_per_second=prepared.mean_motion_face_widths_per_second,
                    face_presence_ratio=prepared.valid_fraction,
                )

    # Policy: the dropout never asked for a reset, but was observed as a loss.
    assert not any(item.should_reset for item in decisions)
    assert [item.transition for item in decisions if item.transition] == ["lost", "reacquired"]

    # Beat ledger: every beat accepted before the dropout is still there.
    assert accepted_before
    accepted_after = {
        event.beat_id
        for event in pipeline.beat_events
        if event.status == BeatStatus.ACCEPTED.value
    }
    assert accepted_before <= accepted_after
    assert len(accepted_after) > len(accepted_before)

    # The window spanning the dropout stayed ready (233 ms gap <= 250 ms).
    spanning = prepare_observation_window(
        buffer.snapshot(since_mono_ns=origin_ns + int((dropout_start - 150) * FRAME_NS)),
        window_seconds=10.0,
        nominal_fps=FPS,
    )
    assert spanning.ready is True
    assert spanning.max_interpolation_gap_ms == pytest.approx(233.3, abs=1.0)
    assert spanning.diagnostics.rejection_counts == {MissingReason.NO_FACE.value: dropout_frames}

    # Blink exposure kept accumulating instead of restarting from zero.
    state = blink.latest_state
    assert state is not None
    assert state.valid_exposure_seconds > exposure_before

    # RGB history retained the pre-dropout observations.
    retained = buffer.snapshot()
    assert retained[0].observed_at_mono_ns < origin_ns + dropout_start * FRAME_NS
    assert any(item.sequence == dropout_start - 1 for item in retained)
    assert last_result is not None and last_result.summary.hr.value == pytest.approx(72.0, abs=2.0)
