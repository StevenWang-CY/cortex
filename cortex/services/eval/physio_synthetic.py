"""Synthetic camera physiology with known ground truth.

The simulator produces what the capture service hands the physiology engine:
a time-stamped RGB skin trace and a face-normalised vertical head position.
Every quantity the engine estimates has a known true value here, so the
qualification harness can measure error instead of asserting plausibility.

The generative model is deliberately simple and explicit:

* beats follow an inter-beat-interval (IBI) process with respiratory sinus
  arrhythmia (RSA), a slower Mayer-wave component and white beat jitter;
* each beat adds a two-lobe pulse (systolic upstroke plus dicrotic wave), so
  the waveform carries the harmonics a real BVP does;
* the pulse modulates R, G and B with the usual blood-volume channel ratios,
  breathing modulates pulse amplitude and moves the head, illumination drifts
  in common mode, and each channel gets independent sensor noise;
* the camera samples at a jittered frame interval and may drop frames.

It is a signal-processing test bench, not a model of a person.  Passing it is
necessary for an estimator to be useful and never sufficient to call it
accurate on people; the reference-sensor datasets cover that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

# Relative pulsatile amplitude per channel (R, G, B) for skin under broadband
# light; green carries the strongest blood-volume signal.
_PULSE_CHANNEL_GAIN = np.asarray([0.33, 0.77, 0.53], dtype=np.float64)
_SKIN_DC = np.asarray([150.0, 110.0, 95.0], dtype=np.float64)


@dataclass(frozen=True)
class SyntheticSubject:
    """Ground-truth physiology and acquisition conditions for one recording."""

    heart_rate_bpm: float = 72.0
    rsa_amplitude_ms: float = 35.0
    mayer_amplitude_ms: float = 20.0
    beat_jitter_ms: float = 6.0
    respiration_rate_bpm: float = 14.0
    heart_rate_drift_bpm_per_min: float = 0.0
    # Acquisition
    duration_seconds: float = 240.0
    fps: float = 30.0
    frame_jitter_ms: float = 2.0
    dropped_frame_probability: float = 0.0
    # Signal strength: pulsatile fraction of the DC skin level (0.3%-1.5% is
    # the realistic range for a webcam after ROI averaging).
    modulation_depth: float = 0.008
    sensor_noise_fraction: float = 0.0015
    illumination_drift_fraction: float = 0.01
    respiration_amplitude_modulation: float = 0.15
    head_motion_face_units: float = 0.004
    head_motion_noise_face_units: float = 0.0015
    seed: int = 0


@dataclass(frozen=True)
class SyntheticRecording:
    """Sampled trace plus the truth needed to score every estimator."""

    subject: SyntheticSubject
    sample_times_s: NDArray[np.float64]
    rgb: NDArray[np.float64]
    head_vertical_face_units: NDArray[np.float64]
    beat_times_s: NDArray[np.float64]
    ibi_ms: NDArray[np.float64] = field(repr=False)

    def true_heart_rate_bpm(self, start_s: float, end_s: float) -> float | None:
        """Mean heart rate from the true beats inside ``[start_s, end_s]``."""

        intervals = self._intervals_between(start_s, end_s)
        if len(intervals) < 2:
            return None
        return float(60_000.0 / np.mean(intervals))

    def true_rmssd_ms(self, start_s: float, end_s: float) -> float | None:
        intervals = self._intervals_between(start_s, end_s)
        if len(intervals) < 3:
            return None
        return float(np.sqrt(np.mean(np.diff(intervals) ** 2)))

    def true_sdnn_ms(self, start_s: float, end_s: float) -> float | None:
        intervals = self._intervals_between(start_s, end_s)
        if len(intervals) < 3:
            return None
        return float(np.std(intervals, ddof=1))

    def _intervals_between(self, start_s: float, end_s: float) -> NDArray[np.float64]:
        starts = self.beat_times_s[:-1]
        ends = self.beat_times_s[1:]
        selected = (starts >= start_s) & (ends <= end_s)
        return np.asarray(self.ibi_ms[selected], dtype=np.float64)


def _beat_times(subject: SyntheticSubject, rng: np.random.Generator) -> NDArray[np.float64]:
    """Integrate the IBI process into beat instants covering the recording."""

    resp_hz = subject.respiration_rate_bpm / 60.0
    mayer_hz = 0.095
    resp_phase = float(rng.uniform(0.0, 2.0 * np.pi))
    mayer_phase = float(rng.uniform(0.0, 2.0 * np.pi))
    times = [float(rng.uniform(0.0, 0.5))]
    lead_out = subject.duration_seconds + 3.0
    while times[-1] < lead_out:
        now = times[-1]
        rate = subject.heart_rate_bpm + subject.heart_rate_drift_bpm_per_min * now / 60.0
        base_ms = 60_000.0 / max(30.0, rate)
        interval_ms = (
            base_ms
            + subject.rsa_amplitude_ms * np.sin(2.0 * np.pi * resp_hz * now + resp_phase)
            + subject.mayer_amplitude_ms * np.sin(2.0 * np.pi * mayer_hz * now + mayer_phase)
            + float(rng.normal(0.0, subject.beat_jitter_ms))
        )
        times.append(now + float(np.clip(interval_ms, 280.0, 2_000.0)) / 1_000.0)
    return np.asarray(times, dtype=np.float64)


def _pulse_waveform(
    sample_times_s: NDArray[np.float64],
    beat_times_s: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Sum a systolic and a dicrotic lobe for every beat, zero mean, unit peak."""

    waveform = np.zeros_like(sample_times_s)
    intervals = np.diff(beat_times_s)
    for beat, interval in zip(beat_times_s[:-1], intervals, strict=True):
        local = sample_times_s - beat
        window = (local > -0.25 * interval) & (local < 1.25 * interval)
        if not bool(window.any()):
            continue
        phase = local[window] / interval
        systolic = np.exp(-0.5 * ((phase - 0.18) / 0.075) ** 2)
        dicrotic = 0.35 * np.exp(-0.5 * ((phase - 0.48) / 0.11) ** 2)
        waveform[window] += systolic + dicrotic
    waveform -= float(np.mean(waveform))
    peak = float(np.max(np.abs(waveform)))
    return waveform / peak if peak > 0 else waveform


def synthesize(subject: SyntheticSubject) -> SyntheticRecording:
    """Render one recording for ``subject`` (deterministic in ``subject.seed``)."""

    if subject.fps <= 0 or subject.duration_seconds <= 0:
        raise ValueError("fps and duration_seconds must be positive")
    rng = np.random.default_rng(subject.seed)
    beat_times = _beat_times(subject, rng)

    frame_count = int(round(subject.duration_seconds * subject.fps))
    nominal = np.arange(frame_count, dtype=np.float64) / subject.fps
    jitter = rng.normal(0.0, subject.frame_jitter_ms / 1_000.0, size=frame_count)
    sample_times = np.maximum.accumulate(nominal + jitter + 1e-6 * np.arange(frame_count))
    if subject.dropped_frame_probability > 0.0:
        keep = rng.random(frame_count) >= subject.dropped_frame_probability
        keep[0] = keep[-1] = True
        sample_times = sample_times[keep]

    resp_hz = subject.respiration_rate_bpm / 60.0
    resp_phase = float(rng.uniform(0.0, 2.0 * np.pi))
    breathing = np.sin(2.0 * np.pi * resp_hz * sample_times + resp_phase)

    pulse = _pulse_waveform(sample_times, beat_times)
    pulse *= 1.0 + subject.respiration_amplitude_modulation * breathing

    # Common-mode illumination: slow drift plus a breathing-locked component
    # (chest and head movement change the shading on the face).
    drift = subject.illumination_drift_fraction * (
        np.sin(2.0 * np.pi * 0.011 * sample_times + float(rng.uniform(0.0, 6.28)))
        + 0.5 * np.sin(2.0 * np.pi * 0.047 * sample_times + float(rng.uniform(0.0, 6.28)))
        + 0.3 * breathing
    )
    rgb = _SKIN_DC[None, :] * (
        1.0
        + drift[:, None]
        + subject.modulation_depth * pulse[:, None] * _PULSE_CHANNEL_GAIN[None, :]
        + rng.normal(0.0, subject.sensor_noise_fraction, size=(len(sample_times), 3))
    )

    head = subject.head_motion_face_units * breathing + rng.normal(
        0.0, subject.head_motion_noise_face_units, size=len(sample_times)
    )
    ibi_ms = np.diff(beat_times) * 1_000.0
    return SyntheticRecording(
        subject=subject,
        sample_times_s=sample_times,
        rgb=np.asarray(rgb, dtype=np.float64),
        head_vertical_face_units=np.asarray(0.5 + head, dtype=np.float64),
        beat_times_s=beat_times,
        ibi_ms=ibi_ms,
    )


def signal_free_recording(
    *,
    duration_seconds: float = 120.0,
    fps: float = 30.0,
    kind: str = "drift",
    seed: int = 0,
) -> SyntheticRecording:
    """A face-coloured trace with no cardiac component at all.

    ``kind`` selects the nuisance: ``"white"`` sensor noise only, ``"drift"``
    slow illumination wander, or ``"pink"`` 1/f noise.  Used to measure how
    often an estimator publishes a value when there is nothing to measure.
    """

    rng = np.random.default_rng(seed)
    count = int(round(duration_seconds * fps))
    times = np.arange(count, dtype=np.float64) / fps
    noise = rng.normal(0.0, 0.0015, size=(count, 3))
    if kind == "drift":
        wander = np.cumsum(rng.normal(0.0, 0.0006, size=count))
        noise = noise + wander[:, None]
    elif kind == "pink":
        spectrum = np.fft.rfft(rng.normal(0.0, 1.0, size=count))
        frequencies = np.fft.rfftfreq(count, d=1.0 / fps)
        frequencies[0] = frequencies[1]
        pink = np.fft.irfft(spectrum / np.sqrt(frequencies), n=count)
        pink = 0.004 * pink / max(float(np.std(pink)), 1e-12)
        noise = noise + pink[:, None]
    elif kind != "white":
        raise ValueError(f"unknown signal-free kind: {kind!r}")
    subject = SyntheticSubject(
        duration_seconds=duration_seconds, fps=fps, modulation_depth=0.0, seed=seed
    )
    return SyntheticRecording(
        subject=subject,
        sample_times_s=times,
        rgb=np.asarray(_SKIN_DC[None, :] * (1.0 + noise), dtype=np.float64),
        head_vertical_face_units=0.5 + rng.normal(0.0, 0.0015, size=count),
        beat_times_s=np.asarray([], dtype=np.float64),
        ibi_ms=np.asarray([], dtype=np.float64),
    )
