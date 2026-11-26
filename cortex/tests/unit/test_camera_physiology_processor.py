"""The shared camera-physiology processor that both the daemon and qualification drive."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import numpy as np
import pytest
from numpy.typing import NDArray

from cortex.libs.config.settings import CortexConfig, RPPGSignalConfig
from cortex.libs.schemas.observations import MissingReason, ObservationValidity
from cortex.services.capture_service.observation_buffer import NumericObservation
from cortex.services.eval.physio_synthetic import (
    SyntheticRecording,
    SyntheticSubject,
    synthesize,
)
from cortex.services.physio_engine.camera_physiology import (
    CameraPhysiologyProcessor,
    PhysiologyUpdate,
    build_rgb_observation_buffer,
    numeric_observation_from_capture,
)
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2

BOOT = UUID("55555555-5555-5555-5555-555555555555")
ORIGIN_NS = 1_000_000_000_000
DATA_FIELDS = (
    "pulse_bpm",
    "pulse_variability_proxy",
    "hrv_sdnn",
    "hrv_pnn50",
    "hrv_sd1",
    "hrv_sd2",
    "hrv_lf_hf_ratio",
    "hrv_sample_entropy",
    "hr_delta_5s",
    "respiration_rate_bpm",
)
# A clean, strong recording: the short 10 s window already carries an
# unmistakable cardiac line, so every phase of window growth is observable.
CLEAN = SyntheticSubject(
    duration_seconds=30.0, modulation_depth=0.02, sensor_noise_fraction=0.0008, seed=5
)


def _rppg() -> RPPGSignalConfig:
    return CortexConfig().signal.rppg


def _processor() -> CameraPhysiologyProcessor:
    config = CortexConfig()
    rppg = config.signal.rppg
    return CameraPhysiologyProcessor(
        rppg,
        nominal_fps=float(config.capture.fps),
        engine=PhysiologyEngineV2(
            rppg,
            max_motion_face_widths_per_second=config.capture.max_motion_face_widths_per_second,
        ),
    )


@dataclass
class Stride:
    at_s: float
    update: PhysiologyUpdate

    @property
    def span_s(self) -> float | None:
        times = self.update.prepared.sample_times_mono_ns
        if times is None:
            return None
        return float(times[-1] - times[0]) / 1e9


@dataclass
class Drive:
    processor: CameraPhysiologyProcessor
    recording: SyntheticRecording
    strides: list[Stride] = field(default_factory=list)


def _drive(
    recording: SyntheticRecording,
    processor: CameraPhysiologyProcessor | None = None,
    *,
    reset_at_s: float | None = None,
) -> Drive:
    """Append every sample and run one stride per second, as the daemon does."""

    proc = processor or _processor()
    buffer = build_rgb_observation_buffer(_rppg())
    result = Drive(processor=proc, recording=recording)
    last_ns = 0
    reset_done = False
    for index, (stamp, rgb) in enumerate(zip(recording.sample_times_s, recording.rgb, strict=True)):
        mono_ns = ORIGIN_NS + int(round(float(stamp) * 1e9))
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=mono_ns // 1_000_000,
                observed_at_mono_ns=mono_ns,
                boot_id=BOOT,
                sequence=index,
                value=np.asarray(rgb, dtype=np.float64),
                validity=ObservationValidity.VALID.value,
                missing_reason=None,
                quality=1.0,
                head_vertical_face_units=float(recording.head_vertical_face_units[index]),
            )
        )
        if last_ns and mono_ns - last_ns < 1_000_000_000:
            continue
        last_ns = mono_ns
        if reset_at_s is not None and not reset_done and stamp >= reset_at_s:
            proc.reset()
            reset_done = True
        result.strides.append(Stride(float(stamp), proc.process(buffer.snapshot(), boot_id=BOOT)))
    return result


@pytest.fixture(scope="module")
def clean_drive() -> Drive:
    return _drive(synthesize(CLEAN))


def test_clean_recording_publishes_heart_rate_within_3_bpm_of_truth(clean_drive: Drive) -> None:
    published = [s for s in clean_drive.strides if s.update.features.pulse_bpm is not None]
    assert len(published) >= 15
    for stride in published:
        features = stride.update.features
        assert features.valid
        evidence = features.pulse_evidence
        assert evidence is not None and evidence.value == features.pulse_bpm
        truth = clean_drive.recording.true_heart_rate_bpm(
            (evidence.window_start_mono_ns - ORIGIN_NS) / 1e9,
            (evidence.window_end_mono_ns - ORIGIN_NS) / 1e9,
        )
        assert truth is not None
        assert features.pulse_bpm == pytest.approx(truth, abs=3.0)


def test_before_the_window_fills_nothing_is_published(clean_drive: Drive) -> None:
    early = [s for s in clean_drive.strides if not s.update.window_ready]
    assert early, "the first strides must precede a ready window"
    assert all(s.at_s < _rppg().window_seconds for s in early)
    for stride in early:
        features = stride.update.features
        assert features.valid is False
        assert stride.update.pulse is None and stride.update.respiration is None
        for name in DATA_FIELDS:
            assert getattr(features, name) is None, name
        assert features.pulse_evidence is None
        assert features.hrv_evidence == {}


def test_window_grows_from_the_short_to_the_long_span(clean_drive: Drive) -> None:
    rppg = _rppg()
    short, long = float(rppg.window_seconds), float(rppg.long_window_seconds)
    long_first_ready = long * rppg.min_valid_coverage
    short_phase = [
        s for s in clean_drive.strides if short + 0.5 <= s.at_s < long_first_ready - 0.5
    ]
    long_phase = [s for s in clean_drive.strides if s.at_s >= long + 0.5]
    assert short_phase and long_phase
    for stride in short_phase:
        assert stride.span_s == pytest.approx(short, abs=0.25)
    for stride in long_phase:
        assert stride.span_s == pytest.approx(long, abs=0.25)
        evidence = stride.update.features.pulse_evidence
        assert evidence is not None and evidence.value is not None
        assert (evidence.window_end_mono_ns - evidence.window_start_mono_ns) / 1e9 == (
            pytest.approx(long, abs=0.25)
        )
    for stride in clean_drive.strides:
        if stride.span_s is not None:
            assert stride.span_s <= stride.at_s + 0.05, "a window never exceeds the data"


def test_heart_rate_delta_needs_an_earlier_published_rate(clean_drive: Drive) -> None:
    published = [s for s in clean_drive.strides if s.update.features.pulse_bpm is not None]
    assert published[0].update.features.hr_delta_5s is None
    later = [s.update.features.hr_delta_5s for s in published[1:]]
    assert any(isinstance(value, float) for value in later)
    for stride in clean_drive.strides:
        if stride.update.features.pulse_bpm is None:
            assert stride.update.features.hr_delta_5s is None


def test_reset_clears_the_processor_history() -> None:
    drive = _drive(synthesize(CLEAN), reset_at_s=20.0)
    before = [s for s in drive.strides if s.at_s < 20.0]
    after = [s for s in drive.strides if s.at_s >= 20.0]
    assert any(s.update.features.hr_delta_5s is not None for s in before)
    first_published_after = next(s for s in after if s.update.features.pulse_bpm is not None)
    assert first_published_after.update.features.hr_delta_5s is None, (
        "the recent heart-rate history must not survive a reset"
    )

    processor = drive.processor
    processor.reset()
    assert processor.latest_respiration is None
    assert processor.engine.pulse.prior_hr_bpm is None
    assert processor.engine.pulse.intervals == ()


def test_processor_rejects_a_non_positive_frame_rate() -> None:
    rppg = _rppg()
    with pytest.raises(ValueError):
        CameraPhysiologyProcessor(rppg, nominal_fps=0.0, engine=PhysiologyEngineV2(rppg))


# ---------------------------------------------------------------------------
# Capture observation -> numeric sample
# ---------------------------------------------------------------------------


@dataclass
class _Observation:
    validity: str
    missing_reason: MissingReason | None = None
    observed_at_unix_ms: int = 1_764_000_000_123
    observed_at_mono_ns: int = 7_000_000_000
    boot_id: UUID = BOOT
    sequence: int = 42
    quality: float = 0.6
    value: Any = field(default_factory=lambda: SimpleNamespace(motion_face_widths_per_second=0.12))


class _Frame:
    def __init__(self, rgb: NDArray[np.float64] | None) -> None:
        self._rgb = rgb

    def combined_rgb(self) -> NDArray[np.float64] | None:
        return self._rgb


class _Extractor:
    def __init__(self, rgb: NDArray[np.float64] | None) -> None:
        self._rgb = rgb
        self.timestamps: list[float] = []

    def extract(self, frame: Any, landmarks_px: Any, timestamp: float) -> _Frame:
        self.timestamps.append(timestamp)
        return _Frame(self._rgb)


def _landmarks() -> NDArray[np.float32]:
    ys = np.linspace(100.0, 300.0, 478, dtype=np.float32)
    xs = np.linspace(200.0, 400.0, 478, dtype=np.float32)
    return np.stack([xs, ys], axis=1)


FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def test_valid_observation_with_a_usable_skin_sample_is_a_valid_numeric_sample() -> None:
    rgb = np.asarray([150.0, 110.0, 95.0])
    extractor = _Extractor(rgb)
    observation = _Observation(validity=ObservationValidity.VALID.value)
    numeric = numeric_observation_from_capture(
        observation=observation, frame=FRAME, landmarks_px=_landmarks(), roi_extractor=extractor
    )
    assert numeric.validity == ObservationValidity.VALID.value
    assert numeric.is_valid
    assert numeric.value is not None and np.array_equal(numeric.value, rgb)
    assert numeric.quality == 0.6
    assert numeric.missing_reason is None
    assert numeric.motion_face_widths_per_second == pytest.approx(0.12)
    assert numeric.head_vertical_face_units is not None
    assert (numeric.observed_at_mono_ns, numeric.boot_id, numeric.sequence) == (
        observation.observed_at_mono_ns,
        BOOT,
        42,
    )
    assert extractor.timestamps == [pytest.approx(observation.observed_at_unix_ms / 1000.0)]


@pytest.mark.parametrize("drop", ["frame", "landmarks"])
def test_valid_observation_without_frame_or_landmarks_is_rejected_as_artifact(drop: str) -> None:
    extractor = _Extractor(np.asarray([150.0, 110.0, 95.0]))
    numeric = numeric_observation_from_capture(
        observation=_Observation(validity=ObservationValidity.VALID.value),
        frame=None if drop == "frame" else FRAME,
        landmarks_px=None if drop == "landmarks" else _landmarks(),
        roi_extractor=extractor,
    )
    assert numeric.validity == ObservationValidity.REJECTED.value
    assert numeric.missing_reason == MissingReason.ARTIFACT
    assert numeric.value is None and numeric.quality == 0.0
    assert numeric.motion_face_widths_per_second is None
    assert numeric.head_vertical_face_units is None
    assert extractor.timestamps == []


@pytest.mark.parametrize(
    "sample",
    [np.asarray([np.nan, 110.0, 95.0]), np.asarray([150.0, np.inf, 95.0]), None],
    ids=["nan", "inf", "no-sample"],
)
def test_unusable_skin_sample_is_rejected_as_occluded(
    sample: NDArray[np.float64] | None,
) -> None:
    numeric = numeric_observation_from_capture(
        observation=_Observation(validity=ObservationValidity.VALID.value),
        frame=FRAME,
        landmarks_px=_landmarks(),
        roi_extractor=_Extractor(sample),
    )
    assert numeric.validity == ObservationValidity.REJECTED.value
    assert numeric.missing_reason == MissingReason.OCCLUDED
    assert numeric.value is None and numeric.quality == 0.0
    assert not numeric.is_valid


def test_already_invalid_observation_keeps_its_reason_and_skips_extraction() -> None:
    extractor = _Extractor(np.asarray([150.0, 110.0, 95.0]))
    numeric = numeric_observation_from_capture(
        observation=_Observation(
            validity=ObservationValidity.REJECTED.value, missing_reason=MissingReason.NO_FACE
        ),
        frame=FRAME,
        landmarks_px=_landmarks(),
        roi_extractor=extractor,
    )
    assert numeric.validity == ObservationValidity.REJECTED.value
    assert numeric.missing_reason == MissingReason.NO_FACE
    assert numeric.value is None and numeric.quality == 0.0
    assert extractor.timestamps == []


# ---------------------------------------------------------------------------
# Observation history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rppg",
    [
        RPPGSignalConfig(),
        RPPGSignalConfig(respiration_window_seconds=60, long_window_seconds=40),
        RPPGSignalConfig(respiration_window_seconds=30, window_seconds=8, long_window_seconds=8),
    ],
    ids=["default", "long-breathing", "short-breathing"],
)
def test_buffer_keeps_at_least_the_breathing_window_at_the_fastest_frame_rate(
    rppg: RPPGSignalConfig,
) -> None:
    buffer = build_rgb_observation_buffer(rppg)
    fps = float(rppg.fps_clamp_max)
    total = int(2 * rppg.respiration_window_seconds * fps)
    for index in range(total):
        mono_ns = ORIGIN_NS + int(round(index / fps * 1e9))
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=mono_ns // 1_000_000,
                observed_at_mono_ns=mono_ns,
                boot_id=BOOT,
                sequence=index,
                value=np.ones(3),
                validity=ObservationValidity.VALID.value,
                missing_reason=None,
                quality=1.0,
            )
        )
    held = buffer.snapshot()
    span = (held[-1].observed_at_mono_ns - held[0].observed_at_mono_ns) / 1e9
    assert span >= rppg.respiration_window_seconds
    assert span >= rppg.long_window_seconds


def test_buffer_honours_a_larger_minimum_item_count() -> None:
    rppg = RPPGSignalConfig()
    buffer = build_rgb_observation_buffer(rppg, minimum_items=20_000)
    for index in range(20_000):  # 20 000 samples inside one second
        mono_ns = ORIGIN_NS + index * 50_000
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=mono_ns // 1_000_000,
                observed_at_mono_ns=mono_ns,
                boot_id=BOOT,
                sequence=index,
                value=np.ones(3),
                validity=ObservationValidity.VALID.value,
                missing_reason=None,
                quality=1.0,
            )
        )
    assert len(buffer) == 20_000
