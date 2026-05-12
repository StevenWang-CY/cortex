"""
Physio Engine — Signal Quality Scorer & Algorithm Switching

Evaluates rPPG signal quality using SNR (peak power to noise floor ratio)
and manages automatic algorithm switching based on quality degradation:

    POS (best accuracy) → CHROM (better skin-tone robustness) → Green (simplest)

When the primary algorithm's quality drops below threshold, the scorer
evaluates the fallback and switches if it produces better results.

Quality metrics:
- SNR: ratio of peak power in cardiac band to noise floor
- Confidence: combined SNR + HR stability scoring
- Per-algorithm tracking for informed switching decisions
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from cortex.libs.signal.peak_detection import compute_signal_quality, estimate_hr_welch
from cortex.services.physio_engine.rppg import RPPGAlgorithm, extract_bvp

logger = logging.getLogger(__name__)

# Quality thresholds for algorithm switching (hysteresis band)
_QUALITY_GOOD = 0.45  # Above this, switch back to preferred algorithm
_QUALITY_POOR = 0.35  # Below this, switch to fallback algorithm
_SWITCH_COOLDOWN_WINDOWS = 5  # Minimum windows before reconsidering


@dataclass(frozen=True)
class QualityAssessment:
    """Quality assessment for a single BVP extraction."""

    algorithm: RPPGAlgorithm
    snr_score: float  # 0-1, in-band power ratio
    hr_bpm: float | None
    hr_confidence: float  # PSD peak prominence
    overall_quality: float  # Combined score 0-1


class QualityScorer:
    """
    Evaluates rPPG signal quality and manages algorithm switching.

    Tracks quality history per algorithm and switches between POS → CHROM →
    Green when quality degrades, with cooldown to prevent oscillation.

    Usage:
        scorer = QualityScorer()
        algorithm = scorer.current_algorithm
        bvp = extract_bvp(rgb_window, algorithm)
        scorer.update(rgb_window, bvp, fs=30.0)
        # scorer.current_algorithm may change
    """

    # Algorithm priority order (best to worst)
    PRIORITY = [RPPGAlgorithm.POS, RPPGAlgorithm.CHROM, RPPGAlgorithm.GREEN]

    def __init__(
        self,
        initial_algorithm: RPPGAlgorithm = RPPGAlgorithm.POS,
        quality_history_size: int = 10,
    ) -> None:
        self._current = initial_algorithm
        self._history_size = quality_history_size

        # Per-algorithm quality history
        self._quality_history: dict[RPPGAlgorithm, deque[float]] = {
            algo: deque(maxlen=quality_history_size)
            for algo in RPPGAlgorithm
        }

        # Switching cooldown
        self._windows_since_switch = 0
        self._latest_assessment: QualityAssessment | None = None

    @property
    def current_algorithm(self) -> RPPGAlgorithm:
        """The currently selected rPPG algorithm."""
        return self._current

    @property
    def latest_assessment(self) -> QualityAssessment | None:
        """The most recent quality assessment."""
        return self._latest_assessment

    def assess_quality(
        self,
        bvp_signal: NDArray[np.float64],
        fs: float = 30.0,
        algorithm: RPPGAlgorithm | None = None,
    ) -> QualityAssessment:
        """
        Assess quality of a BVP signal.

        Args:
            bvp_signal: Extracted BVP signal, shape (N,).
            fs: Sampling frequency.
            algorithm: Which algorithm produced the signal.

        Returns:
            QualityAssessment with SNR, HR, and combined quality.
        """
        algo = algorithm or self._current

        # Compute SNR-based quality
        snr = compute_signal_quality(bvp_signal, fs=fs)

        # Compute HR and confidence
        hr_bpm, hr_confidence = estimate_hr_welch(bvp_signal, fs=fs)

        # Combined quality: weight SNR and HR confidence
        if hr_bpm is not None:
            overall = 0.6 * snr + 0.4 * hr_confidence
        else:
            overall = snr * 0.5  # Penalize if no HR could be estimated

        overall = float(np.clip(overall, 0.0, 1.0))

        return QualityAssessment(
            algorithm=algo,
            snr_score=snr,
            hr_bpm=hr_bpm,
            hr_confidence=hr_confidence,
            overall_quality=overall,
        )

    def update(
        self,
        rgb_window: NDArray[np.float64],
        bvp_signal: NDArray[np.float64],
        fs: float = 30.0,
    ) -> QualityAssessment:
        """
        Update quality tracking and potentially switch algorithms.

        Assesses the current BVP signal quality, records it, and checks
        whether a fallback algorithm would produce better results.

        Args:
            rgb_window: Raw RGB traces, shape (N, 3). Needed for fallback eval.
            bvp_signal: BVP from current algorithm, shape (N,).
            fs: Sampling frequency.

        Returns:
            Quality assessment for the current signal.
        """
        self._windows_since_switch += 1

        # Assess current algorithm
        assessment = self.assess_quality(bvp_signal, fs, self._current)
        self._quality_history[self._current].append(assessment.overall_quality)
        self._latest_assessment = assessment

        # Check if we should consider switching
        if (
            self._windows_since_switch >= _SWITCH_COOLDOWN_WINDOWS
            and assessment.overall_quality < _QUALITY_POOR
        ):
            self._consider_switch(rgb_window, fs)

        # Also try to switch back to a higher-priority algorithm if quality is good
        elif (
            self._windows_since_switch >= _SWITCH_COOLDOWN_WINDOWS * 2
            and self._current != self.PRIORITY[0]
            and assessment.overall_quality >= _QUALITY_GOOD
        ):
            self._consider_upgrade(rgb_window, fs)

        return assessment

    def _consider_switch(
        self,
        rgb_window: NDArray[np.float64],
        fs: float,
    ) -> None:
        """
        Evaluate fallback algorithms when current quality is poor.

        Tries each lower-priority algorithm and switches if one is better.
        """
        current_idx = self.PRIORITY.index(self._current)

        for fallback in self.PRIORITY[current_idx + 1:]:
            fallback_bvp = extract_bvp(rgb_window, algorithm=fallback, fs=fs)
            fallback_assessment = self.assess_quality(fallback_bvp, fs, fallback)

            if fallback_assessment.overall_quality > _QUALITY_POOR:
                logger.info(
                    f"Switching rPPG algorithm: {self._current.value} → {fallback.value} "
                    f"(quality: {self._latest_assessment.overall_quality:.2f} → "
                    f"{fallback_assessment.overall_quality:.2f})"
                )
                self._current = fallback
                self._windows_since_switch = 0
                self._latest_assessment = fallback_assessment
                return

    def _consider_upgrade(
        self,
        rgb_window: NDArray[np.float64],
        fs: float,
    ) -> None:
        """
        Try upgrading to a higher-priority algorithm when conditions improve.
        """
        current_idx = self.PRIORITY.index(self._current)

        for upgrade in self.PRIORITY[:current_idx]:
            upgrade_bvp = extract_bvp(rgb_window, algorithm=upgrade, fs=fs)
            upgrade_assessment = self.assess_quality(upgrade_bvp, fs, upgrade)

            if upgrade_assessment.overall_quality >= _QUALITY_GOOD:
                logger.info(
                    f"Upgrading rPPG algorithm: {self._current.value} → {upgrade.value} "
                    f"(quality: {upgrade_assessment.overall_quality:.2f})"
                )
                self._current = upgrade
                self._windows_since_switch = 0
                return

    def get_mean_quality(self, algorithm: RPPGAlgorithm | None = None) -> float:
        """
        Get the mean quality over recent history for an algorithm.

        Args:
            algorithm: Which algorithm. None uses current.

        Returns:
            Mean quality score, or 0.0 if no history.
        """
        algo = algorithm or self._current
        history = self._quality_history[algo]
        if not history:
            return 0.0
        return float(np.mean(list(history)))

    def reset(self) -> None:
        """Reset all state to defaults."""
        self._current = self.PRIORITY[0]
        for history in self._quality_history.values():
            history.clear()
        self._windows_since_switch = 0
        self._latest_assessment = None
