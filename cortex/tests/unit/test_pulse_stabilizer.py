"""Pulse stabilizer + sampling-rate-correctness coverage (B1 + B2).

Pins the fixes for the "Reading your pulse…" flicker:

* :class:`PulseStabilizer` — Schmitt-trigger lock, last-valid hold across a
  brief dropout, BPM median smoothing + physiological slew limiting.
* per-window ``fs`` threading — feeding the *true* sampling rate makes the
  Welch BPM correct (HR = dominant_freq × 60); the same samples read at a
  wrong fs scale the BPM by the fps ratio.
* :meth:`PulseEstimator.get_features` — the stabilizer holds ``valid=True``
  with the held BPM through a momentary loss instead of flickering to None.
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.signal.peak_detection import estimate_hr_welch
from cortex.services.physio_engine.pulse_estimator import (
    PulseEstimator,
    PulseStabilizer,
)


# --------------------------------------------------------------------------
# PulseStabilizer (B2)
# --------------------------------------------------------------------------
def _stab(**kw) -> PulseStabilizer:
    base = {
        "enter_windows": 1,
        "grace_seconds": 4.0,
        "snr_release_db": 0.0,
        "sqi_release": 0.20,
        "smoothing_seconds": 6.0,
        "max_slew_bpm_per_s": 12.0,
    }
    base.update(kw)
    return PulseStabilizer(**base)


def test_locks_on_first_valid_window() -> None:
    s = _stab()
    out = s.update(timestamp=0.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    assert out.valid is True
    assert out.bpm == pytest.approx(72.0, abs=0.5)
    assert out.held is False


def test_holds_through_brief_dropout() -> None:
    """A marginal window inside the grace window keeps the last valid BPM."""
    s = _stab()
    s.update(timestamp=0.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    # Raw gate fails but quality is only marginal (above release floor) and
    # we are inside the grace window → HOLD, no flicker to "acquiring".
    held = s.update(timestamp=1.0, raw_valid=False, bpm=None, snr_db=1.0, sqi=0.25)
    assert held.valid is True
    assert held.held is True
    assert held.bpm == pytest.approx(72.0, abs=0.5)


def test_releases_after_grace_expires() -> None:
    s = _stab(grace_seconds=4.0)
    s.update(timestamp=0.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    s.update(timestamp=1.0, raw_valid=False, bpm=None, snr_db=1.0, sqi=0.25)
    # 5 s since the last valid window → past grace → honest release.
    released = s.update(timestamp=5.0, raw_valid=False, bpm=None, snr_db=1.0, sqi=0.25)
    assert released.valid is False
    assert released.bpm is None


def test_quality_collapse_releases_immediately() -> None:
    """Quality below the release floor unlocks at once, even within grace."""
    s = _stab()
    s.update(timestamp=0.0, raw_valid=True, bpm=70.0, snr_db=5.0, sqi=0.5)
    out = s.update(timestamp=0.5, raw_valid=False, bpm=None, snr_db=-3.0, sqi=0.05)
    assert out.valid is False
    assert out.bpm is None


def test_slew_limit_rejects_outlier_window() -> None:
    """A single implausible BPM jump is clamped, not reported raw."""
    s = _stab(max_slew_bpm_per_s=12.0, smoothing_seconds=6.0)
    s.update(timestamp=0.0, raw_valid=True, bpm=70.0, snr_db=5.0, sqi=0.5)
    out = s.update(timestamp=1.0, raw_valid=True, bpm=120.0, snr_db=5.0, sqi=0.5)
    # 120 is clamped to 70 + 12*1 = 82, median(70, 82) = 76 — far below 120.
    assert out.bpm < 95.0
    assert out.valid is True


def test_enter_windows_requires_consecutive_valid() -> None:
    s = _stab(enter_windows=3)
    o1 = s.update(timestamp=0.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    o2 = s.update(timestamp=1.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    assert o1.valid is False and o2.valid is False  # still acquiring
    o3 = s.update(timestamp=2.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    assert o3.valid is True  # third consecutive → locked


def test_reset_clears_lock() -> None:
    s = _stab()
    s.update(timestamp=0.0, raw_valid=True, bpm=72.0, snr_db=5.0, sqi=0.5)
    s.reset()
    out = s.update(timestamp=1.0, raw_valid=False, bpm=None, snr_db=1.0, sqi=0.25)
    assert out.valid is False  # nothing to hold after a reset


# --------------------------------------------------------------------------
# Sampling-rate correctness (B1)
# --------------------------------------------------------------------------
def _sinusoid(period_samples: int, n: int = 300) -> np.ndarray:
    t = np.arange(n)
    return np.sin(2.0 * np.pi * t / period_samples).astype(np.float64)


def test_welch_bpm_scales_with_assumed_fs() -> None:
    """The SAME samples read at 24 vs 30 fps yield BPMs in the fps ratio.

    A sinusoid with a 20-sample period is 1.2 Hz (72 bpm) at a true 24 fps
    but 1.5 Hz (90 bpm) if wrongly assumed to be 30 fps — the exact bias the
    hardcoded ``fs=config.capture.fps`` introduced on slower cameras.
    """
    sig = _sinusoid(period_samples=20)
    hr_true, _ = estimate_hr_welch(sig, fs=24.0)
    hr_wrong, _ = estimate_hr_welch(sig, fs=30.0)
    assert hr_true == pytest.approx(72.0, abs=4.0)
    assert hr_wrong == pytest.approx(90.0, abs=4.0)
    # The wrong rate inflates BPM by ~25% (30/24).
    assert hr_wrong / hr_true == pytest.approx(30.0 / 24.0, abs=0.1)


def test_process_window_uses_per_window_fs() -> None:
    """process_window(fs=…) overrides the constructor rate for that window."""
    sig = _sinusoid(period_samples=20)
    est = PulseEstimator(fs=30.0)
    est.process_window(sig, timestamp=0.0, fs=24.0)
    hr24 = est.latest_estimate.hr_bpm
    est2 = PulseEstimator(fs=30.0)
    est2.process_window(sig, timestamp=0.0, fs=30.0)
    hr30 = est2.latest_estimate.hr_bpm
    assert hr24 is not None and hr30 is not None
    assert hr24 < hr30  # the corrected (slower) rate reports a lower BPM
    assert hr24 / hr30 == pytest.approx(24.0 / 30.0, abs=0.12)


# --------------------------------------------------------------------------
# PulseEstimator.get_features integration (B2)
# --------------------------------------------------------------------------
def test_get_features_holds_bpm_through_dropout() -> None:
    """End-to-end: a one-window quality dropout must NOT flip valid→False.

    Drive a clean cardiac signal so the gate locks, then a flat window so the
    raw gate fails. With the stabilizer the reading is held (valid stays
    True, BPM carried); without it the reading would drop to None.
    """
    stab = PulseStabilizer(
        enter_windows=1,
        grace_seconds=4.0,
        snr_release_db=-99.0,  # never collapse on quality for this test
        sqi_release=-1.0,
        smoothing_seconds=6.0,
        max_slew_bpm_per_s=12.0,
    )
    est = PulseEstimator(fs=30.0, stabilizer=stab)
    good = _sinusoid(period_samples=25, n=300)  # 1.2 Hz @ 30 fps = 72 bpm

    est.process_window(good, timestamp=0.0, fs=30.0)
    f_lock = est.get_features(timestamp=0.0)
    assert f_lock.valid is True
    assert f_lock.pulse_bpm is not None
    held_bpm = f_lock.pulse_bpm

    # A flat (DC) window produces no cardiac peak → raw gate fails.
    est.process_window(np.zeros(300, dtype=np.float64), timestamp=1.0, fs=30.0)
    f_hold = est.get_features(timestamp=1.0)
    assert f_hold.valid is True  # held, not flickered to acquiring
    assert f_hold.pulse_bpm == pytest.approx(held_bpm, abs=1.0)


def test_get_features_without_stabilizer_is_stateless() -> None:
    """No stabilizer → legacy behavior (a bad window drops to invalid)."""
    est = PulseEstimator(fs=30.0, stabilizer=None)
    good = _sinusoid(period_samples=25, n=300)
    est.process_window(good, timestamp=0.0, fs=30.0)
    assert est.get_features(timestamp=0.0).valid is True
    est.process_window(np.zeros(300, dtype=np.float64), timestamp=1.0, fs=30.0)
    assert est.get_features(timestamp=1.0).valid is False
