"""
State Engine — Rule-Based Scorer

Computes per-state scores (HYPER, HYPO, FLOW, RECOVERY) from the unified
FeatureVector using configurable weights and user baselines.

Hyper-arousal score formula:
    hyper_score = w1*pulse_elevation + w2*hrv_drop + w3*blink_suppression
                + w4*posture_collapse + w5*mouse_thrash + w6*window_switch
                + w7*workspace_complexity

Default weights: w1=0.20, w2=0.15, w3=0.12, w4=0.08, w5=0.15, w6=0.15, w7=0.15

All sub-scores are normalized to 0-1 range.
"""

from __future__ import annotations

import logging

import numpy as np

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.state import StateScores, UserBaselines

logger = logging.getLogger(__name__)


class RuleScorer:
    """
    Computes state scores from FeatureVector using rule-based sub-scorers.

    Each sub-scorer maps a specific feature (or feature combination) to a
    0-1 score indicating the degree to which that feature suggests a
    particular cognitive state.

    Usage:
        scorer = RuleScorer(baselines=user_baselines)
        scores = scorer.compute_scores(feature_vector)
    """

    def __init__(
        self,
        config: StateConfig | None = None,
        baselines: UserBaselines | None = None,
    ) -> None:
        self._config = config or StateConfig()
        self._baselines = baselines or UserBaselines()
        self._weights = self._config.weights
        # Optional tab category context for same-category discount
        self._tab_categories: list[str] | None = None
        self._apnea_low_since: float | None = None

    @property
    def baselines(self) -> UserBaselines:
        return self._baselines

    @baselines.setter
    def baselines(self, value: UserBaselines) -> None:
        self._baselines = value

    def set_tab_categories(self, categories: list[str] | None) -> None:
        """Set the current tab categories for same-category switching discount.

        When the user is switching between tabs that all belong to the same
        category (e.g., all ``educational``), the window-switching penalty
        should be reduced because the switching is topically coherent.

        Args:
            categories: List of tab type strings (one per open tab),
                or None to disable the discount.
        """
        self._tab_categories = categories

    def _same_category_ratio(self) -> float:
        """Compute the ratio of tabs sharing the most common category.

        Returns 0.0 when no tab categories are set or there is only one tab,
        and approaches 1.0 when all tabs share the same type.
        """
        cats = self._tab_categories
        if not cats or len(cats) < 2:
            return 0.0
        from collections import Counter
        counts = Counter(cats)
        most_common_count = counts.most_common(1)[0][1]
        return most_common_count / len(cats)

    def compute_scores(self, fv: FeatureVector) -> StateScores:
        """
        Compute all state scores from a FeatureVector.

        Args:
            fv: Unified 12-dimensional feature vector.

        Returns:
            StateScores with flow, hypo, hyper, recovery scores.
        """
        hyper = self._compute_hyper_score(fv)
        hypo = self._compute_hypo_score(fv)
        flow = self._compute_flow_score(fv)
        recovery = self._compute_recovery_score(fv, hyper, hypo, flow)

        return StateScores(
            flow=flow,
            hypo=hypo,
            hyper=hyper,
            recovery=recovery,
        )

    def _compute_hyper_score(self, fv: FeatureVector) -> float:
        """
        Compute hyper-arousal (overwhelm) score.

        Weighted sum of 7 sub-scores per spec.
        """
        w = self._weights

        s1 = self.score_pulse_elevation(fv.hr)
        s2 = self.score_hrv_drop(fv.hrv_rmssd)
        s3 = self.score_blink_suppression(fv.blink_rate, hr=fv.hr)
        s4 = self.score_posture_collapse(fv.forward_lean_angle, fv.shoulder_drop_ratio)
        s5 = self.score_mouse_thrash(fv.mouse_velocity_variance)
        s6_switch = self.score_window_switch(fv.tab_switch_frequency)
        s6_thrash = fv.thrashing_score  # 0-1, from focus graph
        # Blend: thrashing_score is more accurate when available
        s6 = (0.6 * s6_thrash + 0.4 * s6_switch) if s6_thrash > 0.1 else s6_switch

        # Same-category discount: switching between tabs of the same type
        # (e.g., all educational) is topically coherent, not thrashing.
        cat_ratio = self._same_category_ratio()
        if cat_ratio > 0.6:
            # Discount proportional to homogeneity (max 50% reduction)
            discount = 0.5 * cat_ratio
            s6 *= (1.0 - discount)
        s7 = self.score_workspace_complexity(fv)

        # Weighted sum (weights should sum to 1.0)
        total = (
            w.pulse_elevation * s1
            + w.hrv_drop * s2
            + w.blink_suppression * s3
            + w.posture_collapse * s4
            + w.mouse_thrashing * s5
            + w.window_switching * s6
            + w.workspace_complexity * s7
        )

        return float(np.clip(total, 0.0, 1.0))

    def _compute_hypo_score(self, fv: FeatureVector) -> float:
        """
        Compute hypo-arousal (under-arousal/disengagement) score.

        Indicators: HR below baseline, HRV dropping, high blink rate,
        mouse drift (low velocity), long pauses, posture slump.
        """
        scores = []

        # HR below baseline
        if fv.hr is not None:
            hr_ratio = fv.hr / self._baselines.hr_baseline
            if hr_ratio < 0.95:
                scores.append(min(1.0, (0.95 - hr_ratio) / 0.15))
            else:
                scores.append(0.0)

        # High blink rate (> 25/min)
        if fv.blink_rate is not None:
            if fv.blink_rate > 25.0:
                scores.append(min(1.0, (fv.blink_rate - 25.0) / 15.0))
            else:
                scores.append(0.0)

        # Low mouse velocity (mouse drift / inactivity)
        if fv.mouse_velocity_mean < 50.0:
            scores.append(0.8)
        elif fv.mouse_velocity_mean < 200.0:
            scores.append(max(0.0, (200.0 - fv.mouse_velocity_mean) / 200.0))
        else:
            scores.append(0.0)

        # Posture slump (shoulder drop without forward lean)
        if fv.shoulder_drop_ratio is not None and fv.shoulder_drop_ratio > 0.1:
            lean = fv.forward_lean_angle or 0.0
            if lean < 15.0:  # Slump without leaning forward
                scores.append(min(1.0, fv.shoulder_drop_ratio / 0.3))
            else:
                scores.append(0.0)

        # Low window switching (minimal engagement)
        if fv.tab_switch_frequency < 2.0:
            scores.append(0.3)
        else:
            scores.append(0.0)

        # Screen apnea indicator (low respiration + fixation)
        apnea = self.score_screen_apnea(
            fv.respiration_rate,
            fv.blink_rate,
            timestamp=fv.timestamp,
        )
        if apnea > 0.3:
            scores.append(apnea)

        if not scores:
            return 0.0

        return float(np.clip(np.mean(scores), 0.0, 1.0))

    def _compute_flow_score(self, fv: FeatureVector) -> float:
        """
        Compute flow (optimal engagement) score.

        Evidence-aligned signature:
        - HR in a narrow band around baseline (moderate arousal)
        - HRV near personal baseline (not stress-low and not boredom-high extremes)
        - Slight blink suppression vs baseline
        - Low correction and low mouse chaos
        - Moderate tab switching (context engagement without thrashing)
        """
        scores = []

        # HR near baseline (+/-8%).
        if fv.hr is not None:
            hr_deviation = abs(fv.hr - self._baselines.hr_baseline) / max(1e-6, self._baselines.hr_baseline)
            if hr_deviation <= 0.08:
                scores.append(1.0 - (hr_deviation / 0.08))
            else:
                scores.append(0.0)

        # HRV around personal center (mu +/- 0.5 sigma).
        if fv.hrv_rmssd is not None:
            hrv_sigma = self._metric_sigma("hrv_rmssd", fallback=max(6.0, self._baselines.hrv_baseline * 0.2))
            z = abs((fv.hrv_rmssd - self._baselines.hrv_baseline) / max(1e-6, hrv_sigma))
            if z <= 0.5:
                scores.append(1.0 - (z / 0.5))
            elif z <= 1.5:
                scores.append(max(0.0, 0.5 - ((z - 0.5) / 2.0)))
            else:
                scores.append(0.0)

        # Mild blink suppression relative to baseline.
        if fv.blink_rate is not None:
            ratio = fv.blink_rate / max(1e-6, self._baselines.blink_rate_baseline)
            if 0.55 <= ratio <= 0.95:
                scores.append(1.0 - abs(ratio - 0.75) / 0.20)
            elif 0.45 <= ratio <= 1.10:
                scores.append(0.4)
            else:
                scores.append(0.0)

        # Low mouse variance (focused, not erratic)
        if fv.mouse_velocity_variance < self._baselines.mouse_variance_baseline * 1.2:
            scores.append(0.8)
        elif fv.mouse_velocity_variance < self._baselines.mouse_variance_baseline * 1.8:
            scores.append(0.4)
        else:
            scores.append(0.0)

        # Low correction rate (if available).
        if fv.correction_rate_per_100_keys is not None:
            if fv.correction_rate_per_100_keys <= 8.0:
                scores.append(0.8)
            elif fv.correction_rate_per_100_keys <= 15.0:
                scores.append(0.4)
            else:
                scores.append(0.0)

        # Moderate tab switching (focused, not thrashing).
        if 0.5 <= fv.tab_switch_frequency <= 4.0:
            scores.append(0.8)
        elif 0.2 <= fv.tab_switch_frequency < 0.5 or 4.0 < fv.tab_switch_frequency <= 6.0:
            scores.append(0.35)
        else:
            scores.append(0.0)

        if not scores:
            return 0.3  # Default slight flow assumption

        return float(np.clip(np.mean(scores), 0.0, 1.0))

    def _compute_recovery_score(
        self, fv: FeatureVector, hyper: float, hypo: float, flow: float,
    ) -> float:
        """
        Compute recovery score.

        Recovery is the transition from HYPER/HYPO back toward FLOW.
        Characterized by mixed signals and declining overwhelm indicators.
        """
        # Recovery should not dominate during already-stable FLOW.
        stress_signal = max(hyper, hypo)
        if 0.15 <= stress_signal <= 0.65 and 0.25 <= flow <= 0.75:
            # HEURISTIC: mixed profile with moderate stress + improving flow.
            recovery = 0.15 + 0.35 * flow + 0.35 * stress_signal
            return float(np.clip(recovery, 0.0, 1.0))

        # Also recovery if hyper is declining
        if 0.4 <= hyper <= 0.7:
            return float(np.clip(0.6 - hyper, 0.0, 1.0))

        return 0.0

    # =========================================================================
    # Sub-score functions (all return 0-1)
    # =========================================================================

    def score_pulse_elevation(self, hr: float | None) -> float:
        """
        Score pulse elevation: HR > baseline + 15%.

        Returns 0-1, where 1.0 = HR 30%+ above baseline.
        """
        if hr is None:
            return 0.0

        sigma = max(1.0, self._baselines.hr_std)
        z = (hr - self._baselines.hr_baseline) / sigma
        if z <= 1.5:
            return 0.0
        # HEURISTIC: map z=[1.5, 4.0] to [0, 1].
        return float(np.clip((z - 1.5) / 2.5, 0.0, 1.0))

    def score_hrv_drop(self, hrv_rmssd: float | None) -> float:
        """
        Score HRV drop: RMSSD < 20ms indicates stress.

        Returns 0-1, where 1.0 = RMSSD near 0.
        """
        if hrv_rmssd is None:
            return 0.0

        sigma = self._metric_sigma("hrv_rmssd", fallback=max(6.0, self._baselines.hrv_baseline * 0.2))
        z = (self._baselines.hrv_baseline - hrv_rmssd) / max(1e-6, sigma)
        if z <= 1.5:
            return 0.0
        # HEURISTIC: map z=[1.5, 3.5] to [0, 1].
        return float(np.clip((z - 1.5) / 2.0, 0.0, 1.0))

    def score_blink_suppression(self, blink_rate: float | None, hr: float | None = None) -> float:
        """
        Score blink suppression: blink rate < 8/min.

        Returns 0-1, where 1.0 = near-zero blinking.
        If HR is within 110% of baseline, attenuate score (concentration, not stress).
        """
        if blink_rate is None:
            return 0.0

        if blink_rate >= 8.0:
            return 0.0

        # Linear from 8 → 0 maps to 0 → 1
        score = (8.0 - blink_rate) / 8.0
        score = float(np.clip(score, 0.0, 1.0))

        # Attenuate if HR is normal — this is concentration, not stress
        if hr is not None and hr <= self._baselines.hr_baseline * 1.10:
            score *= 0.3

        return score

    def score_screen_apnea(
        self,
        respiration_rate: float | None,
        blink_rate: float | None,
        *,
        timestamp: float | None = None,
    ) -> float:
        """
        Score screen apnea: respiration_rate < 8 AND blink suppression.
        Returns 0-1 indicating screen apnea severity.
        """
        if respiration_rate is None:
            return 0.0

        resp_score = 0.0
        if respiration_rate < self._baselines.resp_baseline * 0.5:  # < half baseline
            resp_score = 1.0
        elif respiration_rate < self._baselines.resp_baseline * 0.7:
            resp_score = (self._baselines.resp_baseline * 0.7 - respiration_rate) / (self._baselines.resp_baseline * 0.2)

        # Combine with blink suppression (low blink = fixating = apnea risk)
        blink_score = self.score_blink_suppression(blink_rate)

        # Both must be present for screen apnea
        if resp_score > 0.3 and blink_score > 0.3:
            now = float(timestamp or 0.0)
            if self._apnea_low_since is None:
                self._apnea_low_since = now
            elapsed = max(0.0, now - self._apnea_low_since)
            sustain = min(1.0, elapsed / 30.0)  # HEURISTIC: sustained >=30s.
            return float(np.clip((0.6 * resp_score + 0.4 * blink_score) * sustain, 0.0, 1.0))
        self._apnea_low_since = None
        return 0.0

    def score_posture_collapse(
        self, forward_lean: float | None, shoulder_drop: float | None,
    ) -> float:
        """
        Score posture collapse: forward lean > 20° + shoulder drop.

        Returns 0-1 composite of lean and drop.
        """
        lean_score = 0.0
        drop_score = 0.0

        if forward_lean is not None and forward_lean > 10.0:
            lean_score = min(1.0, (forward_lean - 10.0) / 20.0)

        if shoulder_drop is not None and shoulder_drop > 0.1:
            drop_score = min(1.0, (shoulder_drop - 0.1) / 0.2)

        # Composite: lean contributes more to hyper (forward lean = engagement)
        return float(np.clip(0.7 * lean_score + 0.3 * drop_score, 0.0, 1.0))

    def score_mouse_thrash(self, velocity_variance: float) -> float:
        """
        Score mouse thrashing: velocity variance > 3x baseline.

        Returns 0-1, where 1.0 = extreme erratic movement.
        """
        baseline = self._baselines.mouse_variance_baseline
        if velocity_variance <= baseline:
            return 0.0

        ratio = velocity_variance / baseline
        if ratio <= 3.0:
            # Ramp from 1x to 3x baseline → 0 to 0.5
            return float(np.clip((ratio - 1.0) / 4.0, 0.0, 0.5))

        # Above 3x → 0.5 to 1.0
        score = 0.5 + min(0.5, (ratio - 3.0) / 6.0)
        return float(np.clip(score, 0.0, 1.0))

    def score_window_switch(self, switch_rate: float) -> float:
        """
        Score window switching: > 20 switches/min.

        Returns 0-1, where 1.0 = 40+ switches/min.
        """
        if switch_rate <= 10.0:
            return 0.0

        if switch_rate <= 20.0:
            return float((switch_rate - 10.0) / 20.0 * 0.5)

        # Above 20: 0.5 → 1.0
        score = 0.5 + min(0.5, (switch_rate - 20.0) / 20.0)
        return float(np.clip(score, 0.0, 1.0))

    def score_workspace_complexity(self, fv: FeatureVector) -> float:
        """
        Score workspace complexity from available signals.

        Uses tab count and typing error indicators as proxies.
        Currently uses keystroke_interval_variance as a complexity proxy.
        """
        score = 0.0

        # High keystroke variance suggests debugging (many corrections)
        if fv.keystroke_interval_variance > 5000.0:
            score += 0.3
        elif fv.keystroke_interval_variance > 2000.0:
            score += 0.15

        # High click frequency combined with switching suggests multi-tasking
        if fv.click_frequency > 2.0 and fv.tab_switch_frequency > 10.0:
            score += 0.4
        elif fv.click_frequency > 1.0:
            score += 0.15

        # Mouse velocity high + high variance = searching behavior
        if (fv.mouse_velocity_mean > 1000.0
                and fv.mouse_velocity_variance > self._baselines.mouse_variance_baseline * 2):
            score += 0.3

        if fv.correction_rate_per_100_keys is not None:
            if fv.correction_rate_per_100_keys > 20.0:
                score += 0.35
            elif fv.correction_rate_per_100_keys > 12.0:
                score += 0.2

        if fv.scroll_back_rate_per_min is not None:
            if fv.scroll_back_rate_per_min > 35.0:
                score += 0.25
            elif fv.scroll_back_rate_per_min > 20.0:
                score += 0.12

        return float(np.clip(score, 0.0, 1.0))

    def _metric_sigma(self, metric: str, *, fallback: float) -> float:
        stats = self._baselines.metric_distributions.get(metric, {})
        sigma = float(stats.get("sigma", fallback))
        return max(1e-6, sigma)
