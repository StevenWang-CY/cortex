"""Pulse publication gate: harmonic SNR, confirmed tracks, conjunctive quality, beats.

The gate's job is to publish a rate only where a cardiac line is present;
the product-level guard at the end runs signal-free traces through the same
processor the daemon uses and requires that nothing at all is published.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import numpy as np
import pytest
from numpy.typing import NDArray

from cortex.libs.signal.filters import bandpass_filter
from cortex.services.eval.physio_qualification import (
    METRIC_HEART_RATE,
    METRIC_RESPIRATION,
    METRIC_RMSSD,
    METRIC_SDNN,
    false_publication_rate,
    replay_trace,
)
from cortex.services.eval.physio_synthetic import signal_free_recording
from cortex.services.physio_engine.v2 import pulse as pulse_module
from cortex.services.physio_engine.v2.backends import RPPGBackendRegistry
from cortex.services.physio_engine.v2.beat_timing import timing_noise_correlation
from cortex.services.physio_engine.v2.pulse import (
    HARMONIC_SNR_CONFIRMED_DB,
    SIGNAL_QUALITY_FULL_DB,
    SIGNAL_QUALITY_ZERO_DB,
    TRACK_CONFIRMATION_MAX_AGE_SECONDS,
    TRACK_CONTINUITY_SECONDS,
    TRACK_TOLERANCE_BPM,
    PulsePipelineV2,
    PulseProcessingResult,
    _spectral_hr,
    harmonic_snr_db,
    signal_quality_from_snr,
)

FS = 30.0
BOOT = UUID("66666666-6666-6666-6666-666666666666")
T = np.arange(int(10 * FS), dtype=np.float64) / FS
NO_SIGNAL = "no cardiac signal above the noise floor"
WEAK_DB = 1.5  # between the 0 dB floor and the 3 dB confirmed level
STRONG_DB = 10.0


def _pipeline() -> PulsePipelineV2:
    return PulsePipelineV2(RPPGBackendRegistry.with_packaged_backends().resolve("pos"))


def _rgb(pulse: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.column_stack([100.0 + 0.4 * pulse, 90.0 + 1.5 * pulse, 80.0 + 0.2 * pulse])


def _tone(bpm: float) -> NDArray[np.float64]:
    return np.asarray(np.sin(2.0 * np.pi * bpm / 60.0 * T), dtype=np.float64)


def _window(
    pipeline: PulsePipelineV2,
    pulse: NDArray[np.float64],
    *,
    offset_s: float,
    **kwargs: float,
) -> PulseProcessingResult:
    return pipeline.process_window(
        _rgb(pulse),
        np.rint((T + offset_s) * 1e9).astype(np.int64),
        sample_rate_hz=FS,
        boot_id=BOOT,
        observation_quality=kwargs.pop("observation_quality", 0.95),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Harmonic SNR and its quality mapping
# ---------------------------------------------------------------------------


def _snr(signal: NDArray[np.float64], cardiac_hz: float) -> float:
    return harmonic_snr_db(signal, fs=FS, cardiac_hz=cardiac_hz, low_hz=0.7, high_hz=3.5)


def test_clean_tone_has_a_high_harmonic_snr_with_or_without_a_second_harmonic() -> None:
    fundamental = bandpass_filter(_tone(72.0), fs=FS)
    harmonic_rich = bandpass_filter(_tone(72.0) + 0.6 * _tone(144.0), fs=FS)
    assert _snr(fundamental, 1.2) > 10.0
    # The second harmonic is cardiac power, not noise.
    assert _snr(harmonic_rich, 1.2) > 10.0
    assert _snr(harmonic_rich, 1.2) >= _snr(fundamental, 1.2) - 1.0


def test_white_noise_never_reaches_the_confirmed_level() -> None:
    """Even at the frequency the selector would pick, noise stays below 3 dB."""

    worst = -99.0
    for seed in range(200):
        noise = bandpass_filter(np.random.default_rng(seed).normal(size=T.size), fs=FS)
        peak = _spectral_hr(noise, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        assert peak.hr_bpm is not None
        worst = max(worst, _snr(noise, peak.hr_bpm / 60.0))
    assert worst < HARMONIC_SNR_CONFIRMED_DB


def test_too_short_or_rate_less_window_has_no_snr() -> None:
    assert harmonic_snr_db(_tone(72.0)[:60], fs=FS, cardiac_hz=1.2, low_hz=0.7, high_hz=3.5) == -99.0
    assert _snr(bandpass_filter(_tone(72.0), fs=FS), 0.0) == -99.0


def test_signal_quality_from_snr_endpoints_and_monotonicity() -> None:
    assert signal_quality_from_snr(SIGNAL_QUALITY_ZERO_DB) == 0.0
    assert signal_quality_from_snr(SIGNAL_QUALITY_FULL_DB) == 1.0
    assert signal_quality_from_snr(-99.0) == 0.0
    assert signal_quality_from_snr(99.0) == 1.0
    assert signal_quality_from_snr(float("nan")) == 0.0
    grid = np.linspace(-20.0, 20.0, 401)
    values = np.asarray([signal_quality_from_snr(float(x)) for x in grid])
    assert np.all(np.diff(values) >= 0.0)
    inside = (grid > SIGNAL_QUALITY_ZERO_DB) & (grid < SIGNAL_QUALITY_FULL_DB)
    assert np.all(np.diff(values[inside]) > 0.0)
    # A just-publishable window (at the 0 dB floor) has a small, non-zero quality.
    assert 0.0 < signal_quality_from_snr(0.0) < 0.5


# ---------------------------------------------------------------------------
# Confirmed-track rule for weak windows
#
# The harmonic SNR of a window is replaced by a scripted value so the rule
# can be exercised at exact levels; everything else (spectral rate, nsqi,
# acquisition quality, the ledger) runs for real on a clean tone.
# ---------------------------------------------------------------------------


Script = Callable[[list[float]], None]


@pytest.fixture()
def scripted_snr(monkeypatch: pytest.MonkeyPatch) -> Script:
    queue: list[float] = []

    def _fake(*_args: object, **_kwargs: object) -> float:
        return queue.pop(0)

    monkeypatch.setattr(pulse_module, "harmonic_snr_db", _fake)

    def _load(values: list[float]) -> None:
        queue[:] = list(values)

    return _load


def _published(result: PulseProcessingResult) -> float | None:
    value = result.summary.hr.value
    return None if value is None else float(value)


def test_weak_window_continues_a_recently_confirmed_track(scripted_snr: Script) -> None:
    pipeline = _pipeline()
    scripted_snr([STRONG_DB, WEAK_DB])
    confirmed = _window(pipeline, _tone(72.0), offset_s=0.0)
    weak = _window(pipeline, _tone(72.0), offset_s=1.0)
    assert _published(confirmed) == pytest.approx(72.0, abs=1.0)
    assert weak.sqi_components["continues_track"] == 1.0
    assert _published(weak) == pytest.approx(72.0, abs=1.0)
    assert weak.signal_present


def test_weak_window_without_a_confirmed_track_is_rejected(scripted_snr: Script) -> None:
    pipeline = _pipeline()
    scripted_snr([WEAK_DB])
    result = _window(pipeline, _tone(72.0), offset_s=0.0)
    assert _published(result) is None
    assert result.summary.hr.unavailable_reason == NO_SIGNAL
    assert result.sqi_components["continues_track"] == 0.0
    # The same clean waveform yields no beats once the window is not accepted.
    assert result.candidates == ()


@pytest.mark.parametrize(
    ("next_bpm", "published"),
    [(72.0 + TRACK_TOLERANCE_BPM - 2.0, True), (72.0 + TRACK_TOLERANCE_BPM + 10.0, False)],
    ids=["within-tolerance", "jumped"],
)
def test_weak_window_must_not_jump_the_rate(
    scripted_snr: Script, next_bpm: float, published: bool
) -> None:
    pipeline = _pipeline()
    scripted_snr([STRONG_DB, WEAK_DB])
    _window(pipeline, _tone(72.0), offset_s=0.0)
    result = _window(pipeline, _tone(next_bpm), offset_s=1.0)
    assert (_published(result) is not None) is published
    if not published:
        assert result.summary.hr.unavailable_reason == NO_SIGNAL


def test_weak_window_after_a_track_gap_is_rejected(scripted_snr: Script) -> None:
    pipeline = _pipeline()
    scripted_snr([STRONG_DB, WEAK_DB])
    _window(pipeline, _tone(72.0), offset_s=0.0)
    late = _window(pipeline, _tone(72.0), offset_s=TRACK_CONTINUITY_SECONDS + 1.0)
    assert _published(late) is None


def test_weak_windows_stop_once_the_confirmation_is_too_old(scripted_snr: Script) -> None:
    """A chain of weak windows may extend a track only up to the confirmation age."""

    pipeline = _pipeline()
    stride = 4.5  # keeps every confirmation age off the exact 60 s boundary
    offsets = [stride * k for k in range(int(TRACK_CONFIRMATION_MAX_AGE_SECONDS / stride) + 3)]
    scripted_snr([STRONG_DB] + [WEAK_DB] * (len(offsets) - 1))
    outcomes = [
        (offset, _published(_window(pipeline, _tone(72.0), offset_s=offset)))
        for offset in offsets
    ]
    for offset, value in outcomes:
        # Window ends are offset + the same span, so the confirmation age is the offset.
        if offset <= TRACK_CONFIRMATION_MAX_AGE_SECONDS:
            assert value is not None, f"weak window {offset:.0f} s after confirmation dropped"
        else:
            assert value is None, f"weak window {offset:.0f} s after confirmation published"


def test_a_strong_window_renews_the_confirmation(scripted_snr: Script) -> None:
    pipeline = _pipeline()
    stride = 4.5
    offsets = [stride * k for k in range(30)]  # 0 .. 130.5 s
    renew_at = 13  # 58.5 s, before the first confirmation expires
    scripted_snr([STRONG_DB if k in (0, renew_at) else WEAK_DB for k in range(len(offsets))])
    values = [_published(_window(pipeline, _tone(72.0), offset_s=o)) for o in offsets]
    assert all(value is not None for value in values[: renew_at + 1])
    last_valid = offsets[renew_at] + TRACK_CONFIRMATION_MAX_AGE_SECONDS
    for offset, value in zip(offsets, values, strict=True):
        if offset > offsets[renew_at]:
            assert (value is not None) is (offset <= last_valid), offset


# ---------------------------------------------------------------------------
# Conjunctive window quality
# ---------------------------------------------------------------------------


def test_window_quality_is_signal_strength_times_acquisition_quality() -> None:
    clean = _tone(72.0)
    still = _window(_pipeline(), clean, offset_s=0.0)
    assert still.summary.quality > 0.0
    snr_quality = signal_quality_from_snr(still.sqi_components["harmonic_snr_db"])
    assert still.summary.quality == pytest.approx(snr_quality, rel=1e-9)

    moving = _window(_pipeline(), clean, offset_s=0.0, motion_face_widths_per_second=0.375)
    assert moving.motion_penalty == pytest.approx(0.5)
    assert moving.summary.quality == pytest.approx(0.5 * still.summary.quality, rel=1e-9)

    partial = _window(_pipeline(), clean, offset_s=0.0, face_presence_ratio=0.6)
    assert partial.summary.quality == pytest.approx(0.6 * still.summary.quality, rel=1e-9)

    marginal = _window(_pipeline(), clean, offset_s=0.0, observation_quality=0.2)
    assert marginal.summary.quality == pytest.approx(0.5 * still.summary.quality, rel=1e-9)


def test_exceeding_the_motion_gate_zeroes_quality_and_publication() -> None:
    shaking = _window(_pipeline(), _tone(72.0), offset_s=0.0, motion_face_widths_per_second=0.8)
    assert shaking.sqi_components["acquisition_quality"] == 0.0
    assert shaking.summary.quality == 0.0
    assert _published(shaking) is None


def test_flawless_acquisition_cannot_substitute_for_a_pulse() -> None:
    rng = np.random.default_rng(7)
    noise_rgb = np.asarray([100.0, 90.0, 80.0]) + 0.5 * rng.normal(size=(T.size, 3))
    result = _pipeline().process_window(
        noise_rgb,
        np.rint(T * 1e9).astype(np.int64),
        sample_rate_hz=FS,
        boot_id=BOOT,
        observation_quality=1.0,
        face_presence_ratio=1.0,
    )
    assert result.sqi_components["acquisition_quality"] == pytest.approx(1.0)
    assert result.summary.quality < 0.3
    assert _published(result) is None


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------


def test_beats_come_only_from_windows_that_carry_a_cardiac_line() -> None:
    clean = _window(_pipeline(), _tone(72.0), offset_s=0.0)
    assert clean.signal_present
    assert len(clean.candidates) >= 10
    for seed in range(5):
        rng = np.random.default_rng(seed)
        noise_rgb = np.asarray([100.0, 90.0, 80.0]) + 0.5 * rng.normal(size=(T.size, 3))
        pipeline = _pipeline()
        result = pipeline.process_window(
            noise_rgb,
            np.rint(T * 1e9).astype(np.int64),
            sample_rate_hz=FS,
            boot_id=BOOT,
            observation_quality=1.0,
        )
        assert not result.signal_present
        assert result.candidates == ()
        assert result.beat_events == ()
        assert pipeline.intervals == ()


def test_timing_noise_correlation_is_one_at_lag_zero_and_decays() -> None:
    assert timing_noise_correlation(0.0, cardiac_hz=1.2) == pytest.approx(1.0)
    lags = [0.0, 0.1, 0.25, 0.5, 0.8, 1.0]
    values = [timing_noise_correlation(lag, cardiac_hz=1.2) for lag in lags]
    assert all(later < earlier for earlier, later in zip(values, values[1:], strict=False))
    for lag in (1.5, 2.0, 3.0, 5.0):
        assert abs(timing_noise_correlation(lag, cardiac_hz=1.2)) < 0.2
    # A line so close to the band edge that no beat band fits carries nothing.
    assert timing_noise_correlation(0.0, cardiac_hz=3.9) == 0.0


def test_beat_timing_sigma_falls_as_the_signal_gets_cleaner() -> None:
    medians: list[float] = []
    for noise in (0.6, 0.3, 0.15, 0.05):
        sigmas: list[float] = []
        for seed in range(3):
            rng = np.random.default_rng(100 + seed)
            pulse = _tone(72.0) + noise * rng.normal(size=T.size)
            result = _window(_pipeline(), pulse, offset_s=0.0)
            assert result.signal_present
            sigmas.extend(
                c.timing_sigma_ms for c in result.candidates if c.timing_sigma_ms is not None
            )
        assert sigmas
        medians.append(float(np.median(sigmas)))
    assert all(later < earlier for earlier, later in zip(medians, medians[1:], strict=False))
    # Roughly proportional to the noise amplitude.
    assert medians[0] / medians[-1] > 5.0


# ---------------------------------------------------------------------------
# Product-level guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["white", "drift", "pink"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_signal_free_traces_publish_no_heart_rate_and_no_variability(kind: str, seed: int) -> None:
    recording = signal_free_recording(duration_seconds=100.0, kind=kind, seed=seed)
    records = replay_trace(
        recording.sample_times_s, recording.rgb, recording.head_vertical_face_units
    )
    assert len(records) >= 95
    rates = false_publication_rate(records)
    assert rates[METRIC_HEART_RATE] == 0.0
    assert rates[METRIC_RMSSD] == 0.0
    assert rates[METRIC_SDNN] == 0.0
    # Breathing too: the pulse-amplitude channel abstains without a cardiac
    # line, and head movement alone is one mechanism, not two. (With that
    # abstention forced off, these traces published 14 false breathing rates
    # and every other test stayed green.)
    assert rates[METRIC_RESPIRATION] == 0.0


def test_a_window_below_the_floor_is_rejected_even_on_a_confirmed_track(
    scripted_snr: Script,
) -> None:
    """The 0 dB floor is separate from the confirmed level: a fresh track lets
    a weak window through, not a window with more noise than signal. (With
    the floor removed every other test stayed green.)"""

    pipeline = _pipeline()
    scripted_snr([STRONG_DB, -0.5, 0.5])
    confirmed = _window(pipeline, _tone(72.0), offset_s=0.0)
    below_floor = _window(pipeline, _tone(72.0), offset_s=1.0)
    above_floor = _window(pipeline, _tone(72.0), offset_s=2.0)

    assert _published(confirmed) == pytest.approx(72.0, abs=1.0)
    # Same rate, track one second old, and still not published.
    assert below_floor.sqi_components["continues_track"] == 1.0
    assert _published(below_floor) is None
    assert not below_floor.signal_present
    # Just above the floor the same track carries it.
    assert _published(above_floor) == pytest.approx(72.0, abs=1.0)
