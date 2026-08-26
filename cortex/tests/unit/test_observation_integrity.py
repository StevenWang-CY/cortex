"""WP-2 deterministic tests for camera observation and window integrity."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import CaptureConfig, get_config
from cortex.libs.schemas.features import FrameMeta
from cortex.libs.schemas.observations import (
    CameraIdentity,
    CameraObservationEnvelope,
    MissingReason,
    ObservationEnvelope,
    ObservationSource,
    ObservationValidity,
)
from cortex.services.capture_service.face_tracker import FaceTracker, FaceTrackingResult
from cortex.services.capture_service.observation_buffer import (
    NumericObservation,
    prepare_observation_window,
)
from cortex.services.capture_service.pipeline import CapturePipeline
from cortex.services.capture_service.webcam import (
    CameraSelection,
    CapturedFrame,
    WebcamCapture,
    open_video_capture,
)

_BOOT = UUID("11111111-1111-1111-1111-111111111111")


def _identity(name: str = "FaceTime HD Camera", device_id: int = 0) -> CameraIdentity:
    return CameraSelection(
        device_id=device_id,
        backend=None,
        source="builtin_mac_camera",
        device_name=name,
    ).identity(width=640, height=480)


def _numeric_trace(
    *,
    fps: int = 30,
    seconds: float = 10.0,
    validity: str = ObservationValidity.VALID.value,
    reason: MissingReason | None = None,
) -> list[NumericObservation]:
    count = int(round(fps * seconds))
    interval_ns = int(1_000_000_000 / fps)
    return [
        NumericObservation(
            observed_at_unix_ms=1_000_000 + round(i * 1000 / fps),
            observed_at_mono_ns=5_000_000_000 + i * interval_ns,
            boot_id=_BOOT,
            sequence=i,
            value=(
                np.array([100.0 + i, 90.0 + i, 80.0 + i], dtype=np.float64)
                if validity == ObservationValidity.VALID.value
                else None
            ),
            validity=validity,
            missing_reason=reason,
            quality=1.0 if validity == ObservationValidity.VALID.value else 0.0,
        )
        for i in range(count)
    ]


def _prepare(trace: list[NumericObservation]):
    return prepare_observation_window(
        trace,
        window_seconds=10.0,
        nominal_fps=30.0,
        min_valid_fraction=0.80,
        max_interpolation_gap_ms=250.0,
        max_motion_fraction=0.10,
        fps_clamp_min=10.0,
        fps_clamp_max=60.0,
    )


def _landmarks(*, x_shift: float = 0.0) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            x=0.25 + 0.5 * (i % 20) / 19.0 + x_shift,
            y=0.20 + 0.55 * (i // 20) / 24.0,
            z=0.03,
        )
        for i in range(478)
    ]


def test_observation_envelope_enforces_value_reason_invariant() -> None:
    common = {
        "source": ObservationSource.CAMERA,
        "source_instance_id": uuid4(),
        "sequence": 1,
        "observed_at_unix_ms": 1000,
        "observed_at_mono_ns": 2000,
        "boot_id": _BOOT,
        "quality": 1.0,
        "algorithm_version": "test/1",
    }
    valid = ObservationEnvelope[int](
        **common,
        value=3,
        validity=ObservationValidity.VALID,
    )
    assert valid.value == 3

    with pytest.raises(ValidationError, match="must contain a value"):
        ObservationEnvelope[int](
            **common,
            value=None,
            validity=ObservationValidity.VALID,
        )
    with pytest.raises(ValidationError, match="require a missing_reason"):
        ObservationEnvelope[int](
            **common,
            value=None,
            validity=ObservationValidity.MISSING,
        )


def test_all_missing_window_is_never_ready_and_has_no_values() -> None:
    result = _prepare(
        _numeric_trace(
            validity=ObservationValidity.MISSING.value,
            reason=MissingReason.NO_FACE,
        )
    )
    assert result.ready is False
    assert result.values is None
    assert result.quality == 0.0
    assert MissingReason.NO_FACE in result.unavailable_reasons


def test_single_long_gap_rejects_otherwise_high_coverage_window() -> None:
    trace = _numeric_trace()
    for index in range(100, 111):
        old = trace[index]
        trace[index] = NumericObservation(
            observed_at_unix_ms=old.observed_at_unix_ms,
            observed_at_mono_ns=old.observed_at_mono_ns,
            boot_id=old.boot_id,
            sequence=old.sequence,
            value=None,
            validity=ObservationValidity.MISSING.value,
            missing_reason=MissingReason.FRAME_DROPPED,
            quality=0.0,
        )
    result = _prepare(trace)
    assert result.valid_fraction > 0.90
    assert result.max_interpolation_gap_ms > 250.0
    assert result.ready is False


def test_repeated_timestamp_is_rejected_as_artifact() -> None:
    trace = _numeric_trace()
    repeated = trace[100]
    prior = trace[99]
    trace[100] = NumericObservation(
        observed_at_unix_ms=repeated.observed_at_unix_ms,
        observed_at_mono_ns=prior.observed_at_mono_ns,
        boot_id=repeated.boot_id,
        sequence=repeated.sequence,
        value=repeated.value,
        validity=repeated.validity,
        missing_reason=None,
        quality=repeated.quality,
    )
    result = _prepare(trace)
    assert result.ready is False
    assert MissingReason.ARTIFACT in result.unavailable_reasons


def test_variable_24_fps_trace_meets_80_percent_coverage_boundary() -> None:
    result = _prepare(_numeric_trace(fps=24))
    assert result.ready is True
    assert result.sample_rate_hz == pytest.approx(24.0, rel=0.02)
    assert result.valid_fraction == pytest.approx(0.80)
    assert result.values is not None and np.isfinite(result.values).all()


def test_irregular_capture_times_produce_explicit_uniform_monotonic_grid() -> None:
    trace = _numeric_trace()
    jitter_pattern_ns = (-3_000_000, 2_000_000, 1_000_000, 0)
    irregular: list[NumericObservation] = []
    for index, item in enumerate(trace):
        jitter_ns = jitter_pattern_ns[index % len(jitter_pattern_ns)]
        irregular.append(
            NumericObservation(
                observed_at_unix_ms=item.observed_at_unix_ms,
                observed_at_mono_ns=item.observed_at_mono_ns + jitter_ns,
                boot_id=item.boot_id,
                sequence=item.sequence,
                value=item.value,
                validity=item.validity,
                missing_reason=item.missing_reason,
                quality=item.quality,
                head_vertical_face_units=(
                    0.5 + 0.01 * np.sin(2 * np.pi * index / 120.0)
                ),
            )
        )
    result = _prepare(irregular)
    assert result.ready is True
    assert result.sample_times_mono_ns is not None
    grid_diffs = np.diff(result.sample_times_mono_ns)
    assert bool((grid_diffs > 0).all())
    assert int(np.ptp(grid_diffs)) <= 1
    assert result.head_vertical_face_units is not None
    assert len(result.head_vertical_face_units) == len(result.sample_times_mono_ns)


def test_removing_valid_observations_never_improves_readiness_or_quality() -> None:
    trace = _numeric_trace()
    previous = _prepare(trace)
    for index in (250, 200, 150, 100, 50):
        trace = [item for item in trace if item.sequence != index]
        current = _prepare(trace)
        assert current.quality <= previous.quality + 1e-12
        assert not (current.ready and not previous.ready)
        previous = current


def test_motion_is_computed_before_landmark_commit_in_physical_units() -> None:
    tracker = FaceTracker(CaptureConfig())
    first = tracker._process_detected_face(
        _landmarks(), 480, 640, capture_mono_ns=1_000_000_000
    )
    second = tracker._process_detected_face(
        _landmarks(x_shift=0.02), 480, 640, capture_mono_ns=2_000_000_000
    )
    assert first.nose_displacement_px == 0.0
    assert second.nose_displacement_px > 10.0
    assert second.nose_velocity_px_per_second == pytest.approx(
        second.nose_displacement_px
    )
    assert second.motion_face_widths_per_second is not None
    assert second.motion_face_widths_per_second > 0.0


def test_face_loss_is_elapsed_time_based_and_reacquisition_is_reachable() -> None:
    tracker = FaceTracker(CaptureConfig(face_lost_tolerance_seconds=0.20))
    tracker._process_detected_face(
        _landmarks(), 480, 640, capture_mono_ns=1_000_000_000
    )
    brief = tracker._process_no_face(capture_mono_ns=1_150_000_000)
    lost = tracker._process_no_face(capture_mono_ns=1_250_000_000)
    reacquired = tracker._process_detected_face(
        _landmarks(), 480, 640, capture_mono_ns=1_300_000_000
    )
    assert brief.face_stable is True
    assert lost.face_stable is False
    assert reacquired.face_detected is True
    assert reacquired.face_stable is True


def test_mediapipe_receives_actual_capture_time_and_clamps_repeat_explicitly() -> None:
    tracker = FaceTracker(CaptureConfig())
    result = MagicMock()
    result.face_landmarks = []
    tracker._landmarker = MagicMock()
    tracker._landmarker.detect_for_video.return_value = result
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    first = tracker.process_frame(frame, capture_mono_ns=12_345_678_901)
    second = tracker.process_frame(frame, capture_mono_ns=12_345_678_901)
    timestamps = [call.args[1] for call in tracker._landmarker.detect_for_video.call_args_list]
    assert timestamps == [12_345, 12_346]
    assert first.detector_timestamp_adjusted is False
    assert second.detector_timestamp_adjusted is True


def test_pipeline_emits_missing_no_face_low_light_and_intentional_skip() -> None:
    clock = FakeClock(wall_unix_ms=100_000, mono_ns=5_000_000_000)
    pipeline = CapturePipeline(CaptureConfig(), clock=clock)
    identity = _identity()
    source_id = uuid4()
    missing = CapturedFrame(
        frame=None,
        timestamp=100.0,
        sequence=0,
        observed_at_unix_ms=100_000,
        observed_at_mono_ns=5_000_000_000,
        boot_id=clock.boot_id,
        source_instance_id=source_id,
        camera_identity=identity,
        missing_reason=MissingReason.CAMERA_WARMUP,
    )
    missing_output = pipeline._process_frame(missing)
    assert missing_output.observation.validity == ObservationValidity.MISSING.value
    assert missing_output.observation.missing_reason == MissingReason.CAMERA_WARMUP.value

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    captured = CapturedFrame(
        frame=frame,
        timestamp=100.033,
        sequence=1,
        observed_at_unix_ms=100_033,
        observed_at_mono_ns=5_033_000_000,
        boot_id=clock.boot_id,
        source_instance_id=source_id,
        camera_identity=identity,
    )
    no_face = FaceTrackingResult(
        face_detected=False,
        confidence=0.0,
        landmarks=None,
        landmarks_px=None,
        bounding_box=None,
        face_stable=False,
        observed_at_mono_ns=5_033_000_000,
    )
    pipeline._face_tracker.process_frame = MagicMock(return_value=no_face)  # type: ignore[method-assign]
    no_face_output = pipeline._process_frame(captured)
    assert no_face_output.observation.missing_reason == MissingReason.NO_FACE.value
    assert no_face_output.frame_meta.frame_available is True

    tracked = FaceTrackingResult(
        face_detected=True,
        confidence=0.9,
        landmarks=np.zeros((478, 3), dtype=np.float32),
        landmarks_px=np.zeros((478, 2), dtype=np.float32),
        bounding_box=None,
        face_stable=True,
        observed_at_mono_ns=5_033_000_000,
        motion_face_widths_per_second=0.0,
    )
    pipeline._face_tracker.process_frame = MagicMock(return_value=tracked)  # type: ignore[method-assign]
    low_light = pipeline._process_frame(captured)
    assert low_light.observation.validity == ObservationValidity.REJECTED.value
    assert low_light.observation.missing_reason == MissingReason.LOW_LIGHT.value

    skipped = pipeline._missing_output(
        captured,
        validity=ObservationValidity.REJECTED,
        reason=MissingReason.FRAME_DROPPED,
    )
    assert skipped.observation.missing_reason == MissingReason.FRAME_DROPPED.value
    assert skipped.frame_meta.frame_available is False
    assert skipped.frame_meta.missing_reason == MissingReason.FRAME_DROPPED


def test_frame_meta_rejects_incoherent_frame_availability() -> None:
    """Pixel availability, missing reason, and face state form one invariant."""

    common = {
        "timestamp": 1_800_000_000.0,
        "face_detected": False,
        "face_confidence": 0.0,
        "brightness_score": 0.0,
        "blur_score": 0.0,
        "motion_score": 0.0,
    }

    with pytest.raises(ValueError, match="missing frames require a missing_reason"):
        FrameMeta(frame_available=False, **common)

    with pytest.raises(ValueError, match="available frames cannot carry"):
        FrameMeta(
            frame_available=True,
            missing_reason=MissingReason.SOURCE_DISCONNECTED,
            **common,
        )

    with pytest.raises(ValueError, match="cannot report a detected face"):
        FrameMeta(
            frame_available=False,
            missing_reason=MissingReason.SOURCE_DISCONNECTED,
            **{**common, "face_detected": True},
        )


@pytest.mark.asyncio
async def test_webcam_failed_read_and_success_are_both_sequenced_observations() -> None:
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.side_effect = [
        (False, None),
        (True, np.zeros((48, 64, 3), dtype=np.uint8)),
        (True, np.zeros((48, 64, 3), dtype=np.uint8)),
    ]
    selection = CameraSelection(0, None, "builtin_mac_camera", "FaceTime HD Camera")
    capture = WebcamCapture(CaptureConfig(fps=30), queue_maxsize=8)
    with patch(
        "cortex.services.capture_service.webcam.open_video_capture",
        return_value=(cap, selection),
    ):
        await capture.start()
        try:
            first = await capture.get_frame(timeout=1.0)
            second = await capture.get_frame(timeout=1.0)
        finally:
            await capture.stop()
    assert first is not None and first.frame is None
    assert first.missing_reason == MissingReason.CAMERA_WARMUP
    assert second is not None and second.frame is not None
    assert (first.sequence, second.sequence) == (0, 1)
    assert first.observed_at_mono_ns is not None
    assert second.observed_at_mono_ns is not None


def test_camera_identity_survives_index_reorder_but_changes_with_device() -> None:
    first = _identity(device_id=0)
    reordered = _identity(device_id=3)
    external = _identity(name="Logitech BRIO", device_id=0)
    assert first.identity_key == reordered.identity_key
    assert first.device_id != reordered.device_id
    assert first.identity_key != external.identity_key


def test_post_open_continuity_wake_is_rejected_and_live_name_is_exposed() -> None:
    iphone = MagicMock()
    iphone.isOpened.return_value = True
    iphone.read.return_value = (True, np.zeros((48, 64, 3), dtype=np.uint8))
    builtin = MagicMock()
    builtin.isOpened.return_value = True
    builtin.read.return_value = (True, np.zeros((48, 64, 3), dtype=np.uint8))
    candidates = [
        CameraSelection(0, None, "builtin_mac_camera", "FaceTime HD Camera"),
        CameraSelection(1, None, "other_camera", "USB Camera"),
    ]
    with (
        patch("cortex.services.capture_service.webcam.is_macos", return_value=True),
        patch(
            "cortex.services.capture_service.webcam._macos_camera_permission_is_authorized",
            return_value=True,
        ),
        patch(
            "cortex.services.capture_service.webcam._iter_camera_candidates",
            return_value=iter(candidates),
        ),
        patch(
            "cortex.services.capture_service.webcam._list_macos_video_device_names",
            return_value=["Test User's iPhone Camera", "FaceTime HD Camera"],
        ),
        patch(
            "cortex.services.capture_service.webcam.cv2.VideoCapture",
            side_effect=[iphone, builtin],
        ),
        patch("cortex.services.capture_service.webcam.time.sleep"),
    ):
        cap, selection = open_video_capture(CaptureConfig())
    assert cap is builtin
    assert selection is not None
    assert selection.device_id == 1
    assert selection.device_name == "FaceTime HD Camera"
    iphone.release.assert_called_once()


def test_warmup_retries_all_four_reads_before_accepting_camera() -> None:
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.side_effect = [
        (False, None),
        (False, None),
        (False, None),
        (True, np.zeros((48, 64, 3), dtype=np.uint8)),
    ]
    candidate = CameraSelection(0, None, "configured_device", "USB Camera")
    with (
        patch("cortex.services.capture_service.webcam.is_macos", return_value=False),
        patch(
            "cortex.services.capture_service.webcam._iter_camera_candidates",
            return_value=iter([candidate]),
        ),
        patch("cortex.services.capture_service.webcam.cv2.VideoCapture", return_value=cap),
        patch("cortex.services.capture_service.webcam.time.sleep"),
    ):
        opened, _ = open_video_capture(CaptureConfig(device_id=0))
    assert opened is cap
    assert cap.read.call_count == 4


@pytest.mark.asyncio
async def test_stop_releases_camera_even_when_not_running_and_release_raises() -> None:
    capture = WebcamCapture()
    cap = MagicMock()
    cap.release.side_effect = RuntimeError("injected release failure")
    capture._cap = cap
    await capture.stop()
    cap.release.assert_called_once()
    assert capture._cap is None


def test_runtime_camera_identity_never_implies_calibration_without_profile() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=get_config())
    source_id = uuid4()

    def envelope(identity: CameraIdentity, sequence: int):
        return ObservationEnvelope[dict[str, str]](
            source=ObservationSource.CAMERA,
            source_instance_id=source_id,
            sequence=sequence,
            observed_at_unix_ms=1000 + sequence,
            observed_at_mono_ns=2000 + sequence,
            boot_id=daemon._clock.boot_id,
            value={"identity": identity.identity_key},
            validity=ObservationValidity.VALID,
            quality=1.0,
            algorithm_version="test/1",
        )

    first = _identity()
    second = _identity(name="Logitech BRIO")
    daemon._handle_camera_identity(first, envelope(first, 0))
    assert daemon._camera_calibration_valid is False
    daemon._handle_camera_identity(second, envelope(second, 1))
    assert daemon._camera_calibration_valid is False
    assert len(daemon._rgb_observations) == 0


@pytest.mark.asyncio
async def test_runtime_never_calls_pulse_estimator_for_all_missing_window() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    cfg = get_config().model_copy(deep=True)
    cfg.capture.fps = 10
    cfg.signal.rppg.window_seconds = 1
    cfg.signal.rppg.stride_seconds = 1
    daemon = CortexDaemon(config=cfg)
    identity = _identity()
    source_id = uuid4()
    process_window = MagicMock()
    daemon._pulse_estimator.process_window = process_window  # type: ignore[method-assign]

    with patch.object(daemon, "_services"):
        for sequence in range(12):
            unix_ms = 100_000 + sequence * 100
            mono_ns = 5_000_000_000 + sequence * 100_000_000
            observation = CameraObservationEnvelope(
                source=ObservationSource.CAMERA,
                source_instance_id=source_id,
                sequence=sequence,
                observed_at_unix_ms=unix_ms,
                observed_at_mono_ns=mono_ns,
                boot_id=daemon._clock.boot_id,
                value=None,
                validity=ObservationValidity.MISSING,
                missing_reason=MissingReason.NO_FACE,
                quality=0.0,
                algorithm_version="test/1",
            )
            output = SimpleNamespace(
                frame_meta=SimpleNamespace(
                    timestamp=unix_ms / 1000.0,
                    face_detected=False,
                    face_confidence=0.0,
                    brightness_score=0.0,
                    blur_score=0.0,
                    motion_score=0.0,
                    low_quality=True,
                ),
                frame=None,
                landmarks_px=None,
                tracking=SimpleNamespace(face_stable=False, is_replayed=False),
                camera_identity=identity,
                observation=observation,
            )
            await daemon._process_capture_output(output)

    process_window.assert_not_called()
    assert daemon._latest_physio.valid is False
    assert daemon._latest_physio.pulse_bpm is None
