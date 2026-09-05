"""Hardware-free end-to-end pulse test: RGB trace -> window gate -> pipeline.

A synthetic RGB trace with a known heart rate, baseline drift and noise is
scheduled as camera observations at several frame rates (with and without a
steady 10 % rejection rate), gated and resampled by
``prepare_observation_window`` and processed by ``PulsePipelineV2`` window by
window.  The published HR must track the truth within 2 BPM and the
overlap-reconciled beat ledger must reproduce the true inter-beat interval.
"""

from __future__ import annotations

from uuid import UUID

import numpy as np
import pytest

from cortex.libs.schemas.observations import MissingReason, ObservationValidity
from cortex.libs.schemas.physiology import BeatCandidate, BeatStatus
from cortex.services.capture_service.observation_buffer import (
    NumericObservation,
    ObservationBuffer,
    prepare_observation_window,
)
from cortex.services.physio_engine.v2.backends import RPPGBackendRegistry
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.pulse import PulsePipelineV2

_BOOT = UUID("44444444-4444-4444-4444-444444444444")
TRUE_HR_BPM = 70.0
WINDOW_SECONDS = 10.0
STRIDE_NS = 1_000_000_000


def _synthetic_rgb(fps: float, seconds: float, *, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = int(round(seconds * fps))
    t = np.arange(count, dtype=np.float64) / fps
    pulse = np.sin(2 * np.pi * TRUE_HR_BPM / 60.0 * t)
    pulse += 0.25 * np.sin(2 * np.pi * 2 * TRUE_HR_BPM / 60.0 * t)
    drift = 3.0 * np.sin(2 * np.pi * 0.05 * t) + 1.5 * np.sin(2 * np.pi * 0.21 * t)
    noise = rng.normal(0.0, 0.15, size=(count, 3))
    rgb = np.column_stack(
        [
            100.0 + 0.4 * pulse + drift,
            90.0 + 1.5 * pulse + 0.8 * drift,
            80.0 + 0.2 * pulse + 1.2 * drift,
        ]
    )
    return np.asarray(rgb + noise, dtype=np.float64)


def _run(fps: float, reject_fraction: float, *, seconds: float = 40.0):
    rgb = _synthetic_rgb(fps, seconds)
    interval_ns = int(round(1e9 / fps))
    buffer: ObservationBuffer[NumericObservation] = ObservationBuffer(
        max_age_seconds=WINDOW_SECONDS + 2.0, max_items=8000
    )
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    pipeline = PulsePipelineV2(backend)
    reject_every = int(round(1.0 / reject_fraction)) if reject_fraction > 0 else 0
    published: list[float | None] = []
    last_update_ns: int | None = None
    last_result = None
    for index in range(len(rgb)):
        rejected = reject_every > 0 and index % reject_every == 5
        mono_ns = 7_000_000_000 + index * interval_ns
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=1_000_000 + index * 33,
                observed_at_mono_ns=mono_ns,
                boot_id=_BOOT,
                sequence=index,
                value=None if rejected else rgb[index],
                validity=(
                    ObservationValidity.REJECTED.value
                    if rejected
                    else ObservationValidity.VALID.value
                ),
                missing_reason=MissingReason.LOW_LIGHT if rejected else None,
                quality=0.0 if rejected else 0.9,
                motion_face_widths_per_second=None if rejected else 0.1,
            )
        )
        if last_update_ns is not None and mono_ns - last_update_ns < STRIDE_NS:
            continue
        last_update_ns = mono_ns
        prepared = prepare_observation_window(
            buffer.snapshot(),
            window_seconds=WINDOW_SECONDS,
            nominal_fps=fps,
        )
        if not prepared.ready:
            continue
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
        published.append(last_result.summary.hr.value)
    assert last_result is not None
    return published, last_result


@pytest.mark.parametrize("fps", [30.0, 24.0, 15.0])
@pytest.mark.parametrize("reject_fraction", [0.0, 0.10])
def test_known_heart_rate_survives_scheduling_gating_and_resampling(
    fps: float, reject_fraction: float
) -> None:
    published, last = _run(fps, reject_fraction)
    assert len(published) >= 25
    values = [value for value in published if value is not None]
    # Every window that reached the gate must have been published...
    assert len(values) == len(published)
    # ...within 2 BPM of the truth.
    assert max(abs(value - TRUE_HR_BPM) for value in values) <= 2.0

    accepted = [
        interval.duration_ms
        for interval in last.intervals
        if interval.status == BeatStatus.ACCEPTED.value
    ]
    assert len(accepted) >= 30
    assert float(np.mean(accepted)) == pytest.approx(60_000.0 / TRUE_HR_BPM, abs=20.0)
    assert last.summary.quality > 0.3


def test_same_beat_across_ten_overlapping_windows_is_one_accepted_event() -> None:
    ledger = BeatLedger()
    beat_ns = 7_000_000_000
    # Sub-sample jitter at 30 fps is below +/-17 ms; use a deterministic
    # spread inside that band across ten windows that all contain the beat
    # in their interior (no window boundary within 750 ms of it).
    jitters_ms = (0, 3, -5, 8, -11, 14, -2, 6, -9, 12)
    events = ()
    for index, jitter_ms in enumerate(jitters_ms):
        window_id = f"w{index}"
        start_ns = index * 500_000_000
        end_ns = start_ns + 10_000_000_000
        candidate = BeatCandidate(
            candidate_id=f"{window_id}-beat",
            absolute_mono_ns=beat_ns + jitter_ms * 1_000_000,
            prominence=1.0,
            quality=0.9,
            source_window_id=window_id,
            near_window_boundary=False,
        )
        events, intervals = ledger.ingest(
            [candidate],
            window_id=window_id,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boundary_margin_ns=750_000_000,
        )
        assert intervals == ()
    accepted = [event for event in events if event.status == BeatStatus.ACCEPTED.value]
    assert len(events) == 1
    assert len(accepted) == 1
    assert len(accepted[0].source_window_ids) == 10
    assert abs(accepted[0].absolute_mono_ns - beat_ns) <= 15_000_000
