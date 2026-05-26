"""
State Engine — Causal Attribution (P0 §3.9).

Maps a live :class:`FeatureVector` into the structured causal rationale
(:class:`CausalSignal`) the UI renders inside the "Why?" drilldown.

The algorithm is pure surfacing — no new computation:

1. For each tracked signal (HRV, HR, blink rate, tab switches, forward
   lean, PERCLOS, scroll-back) the attributor computes a z-score
   against the user's baseline using the personal ``metric_distributions``
   when available and conservative defaults otherwise.
2. The 60-sample 1-Hz sparkline buffer is the most recent N seconds of
   raw values streamed through ``record_feature_vector``; the daemon
   feeds the attributor at the same cadence the state loop runs.
3. The top three signals by |z-score| are returned, ranked
   primary → secondary → tertiary. Signals below a minimum |z| floor
   are dropped so noise doesn't pollute the rationale.

Privacy: the sparkline buffers carry per-second aggregates only; raw
frame data never enters this module.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from cortex.libs.schemas.intervention import CausalSignal
from cortex.libs.schemas.state import UserBaselines

logger = logging.getLogger(__name__)

# 60-sample 1-Hz buffers per signal — exactly one minute of context.
_SPARKLINE_DEPTH = 60

# Drop signals whose absolute z-score is below this floor — keeps
# noise out of the rationale (a tab-switch baseline of 2 ± 1.5 will not
# fire a "tab thrashing" callout when current value is 3).
_MIN_ABS_Z = 0.6

# Direction of "abnormal" for each signal. ``below`` means "low values
# are abnormal" (HRV drop → stress); ``above`` means "high values are
# abnormal" (more tab switches → thrashing); ``abs`` means deviation in
# either direction is interesting.
_Direction = str  # "below" | "above" | "abs"


@dataclass(frozen=True)
class _SignalConfig:
    """Per-signal extraction + normalization config."""

    label: str
    unit: str
    feature_attr: str  # FeatureVector field name
    baseline_attr: str  # UserBaselines field name (or "" for fixed default)
    baseline_default: float
    sigma_default: float
    metric_key: str  # key into UserBaselines.metric_distributions
    direction: _Direction


_SIGNAL_CONFIG: dict[str, _SignalConfig] = {
    "hrv": _SignalConfig(
        label="HRV",
        unit="ms",
        feature_attr="hrv_rmssd",
        baseline_attr="hrv_baseline",
        baseline_default=50.0,
        sigma_default=8.0,
        metric_key="hrv_rmssd",
        direction="below",
    ),
    "hr": _SignalConfig(
        label="Heart rate",
        unit="bpm",
        feature_attr="hr",
        baseline_attr="hr_baseline",
        baseline_default=72.0,
        sigma_default=6.0,
        metric_key="hr",
        direction="above",
    ),
    "blink": _SignalConfig(
        label="Blink rate",
        unit="/min",
        feature_attr="blink_rate",
        baseline_attr="blink_rate_baseline",
        baseline_default=17.0,
        sigma_default=4.0,
        metric_key="blink_rate",
        direction="abs",
    ),
    "tab_switches": _SignalConfig(
        label="Tab switches",
        unit="/min",
        feature_attr="tab_switch_frequency",
        baseline_attr="",
        baseline_default=2.0,
        sigma_default=2.0,
        metric_key="tab_switch_frequency",
        direction="above",
    ),
    "forward_lean": _SignalConfig(
        label="Forward lean",
        unit="°",
        feature_attr="forward_lean_angle",
        baseline_attr="",
        baseline_default=4.0,
        sigma_default=6.0,
        metric_key="forward_lean_angle",
        direction="above",
    ),
    "perclos": _SignalConfig(
        label="Eye closure",
        unit="",
        feature_attr="perclos_60s",
        baseline_attr="",
        baseline_default=0.10,
        sigma_default=0.08,
        metric_key="perclos_60s",
        direction="above",
    ),
    "scroll_back": _SignalConfig(
        label="Re-read bursts",
        unit="/min",
        feature_attr="scroll_back_rate_per_min",
        baseline_attr="",
        baseline_default=0.5,
        sigma_default=1.0,
        metric_key="scroll_back_rate_per_min",
        direction="above",
    ),
}


class CausalAttributor:
    """Stateful attributor — owns the per-signal sparkline buffers.

    The daemon feeds it a :class:`FeatureVector` once per state-loop
    tick (~2 Hz); the attributor extracts each tracked signal, appends
    the raw 1-Hz aggregate to that signal's bounded deque, and on
    request computes the top-three structured rationale.
    """

    def __init__(self, depth: int = _SPARKLINE_DEPTH) -> None:
        self._depth = max(10, int(depth))
        self._buffers: dict[str, deque[float]] = {
            key: deque(maxlen=self._depth) for key in _SIGNAL_CONFIG
        }

    def record_feature_vector(self, features: object) -> None:
        """Append the current value of every tracked signal."""
        for key, cfg in _SIGNAL_CONFIG.items():
            value = self._extract(features, cfg.feature_attr)
            if value is None:
                continue
            self._buffers[key].append(float(value))

    def reset(self) -> None:
        """Clear every sparkline buffer (e.g. on baseline re-cal)."""
        for buf in self._buffers.values():
            buf.clear()

    def attribute_top_signals(
        self,
        features: object,
        baselines: UserBaselines | None = None,
        *,
        max_signals: int = 3,
    ) -> list[CausalSignal]:
        """Return the top-N causal signals ranked by |z-score|.

        When no signal exceeds the :data:`_MIN_ABS_Z` floor (e.g. the
        user is well within their personal envelope) the method
        synthesises a single ``CausalSignal`` named "Baseline" so the
        "Why?" drilldown UI can render a meaningful explanation
        instead of a blank panel. This closes the audit gap where the
        empty-list response looked like a bug rather than a healthy
        baseline reading.
        """
        scored: list[tuple[float, CausalSignal]] = []
        for key, cfg in _SIGNAL_CONFIG.items():
            value = self._extract(features, cfg.feature_attr)
            if value is None:
                continue
            baseline, sigma = self._baseline_for(cfg, baselines)
            z = self._z_score(float(value), baseline, sigma, cfg.direction)
            if abs(z) < _MIN_ABS_Z:
                continue
            delta_pct: float | None = None
            if baseline and abs(baseline) > 1e-6:
                delta_pct = (float(value) - baseline) / abs(baseline) * 100.0
            samples = list(self._buffers[key])
            scored.append((
                abs(z),
                CausalSignal(
                    name=cfg.label,
                    current_value=float(value),
                    baseline_value=float(baseline) if baseline is not None else None,
                    unit=cfg.unit,
                    delta_pct=delta_pct,
                    samples_60s=samples,
                    severity="secondary",  # set after sorting
                ),
            ))

        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[: max(1, int(max_signals))]
        ranked: list[CausalSignal] = []
        for idx, (_z, sig) in enumerate(top):
            if idx == 0:
                severity = "primary"
            elif idx == 1:
                severity = "secondary"
            else:
                severity = "tertiary"
            ranked.append(sig.model_copy(update={"severity": severity}))

        # P0 §3.9 audit fix: empty result → synthetic baseline signal.
        # Without this the UI shows a blank "Why?" panel which reads
        # like a bug. Surface the HRV reading (or, failing that, HR)
        # as a neutral baseline so the user sees "All signals within
        # normal range" instead of nothing.
        if not ranked:
            for key in ("hrv", "hr"):
                cfg = _SIGNAL_CONFIG[key]
                value = self._extract(features, cfg.feature_attr)
                if value is None:
                    continue
                baseline, _sigma = self._baseline_for(cfg, baselines)
                samples = list(self._buffers[key])
                ranked.append(
                    CausalSignal(
                        name=f"{cfg.label} (baseline)",
                        current_value=float(value),
                        baseline_value=(
                            float(baseline) if baseline is not None else None
                        ),
                        unit=cfg.unit,
                        delta_pct=None,
                        samples_60s=samples,
                        severity="primary",
                    )
                )
                break
        return ranked

    @staticmethod
    def _extract(features: object, attr: str) -> float | None:
        value = getattr(features, attr, None)
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(v) or math.isinf(v):
            return None
        return v

    @staticmethod
    def _baseline_for(
        cfg: _SignalConfig, baselines: UserBaselines | None,
    ) -> tuple[float, float]:
        baseline = cfg.baseline_default
        sigma = cfg.sigma_default
        if baselines is None:
            return baseline, sigma
        if cfg.baseline_attr:
            attr_val = getattr(baselines, cfg.baseline_attr, None)
            if attr_val is not None:
                try:
                    baseline = float(attr_val)
                except (TypeError, ValueError):
                    pass
        dist = baselines.metric_distributions.get(cfg.metric_key, {}) if baselines else {}
        if "mu" in dist:
            try:
                baseline = float(dist["mu"])
            except (TypeError, ValueError):
                pass
        if "sigma" in dist:
            try:
                cand = float(dist["sigma"])
                if cand > 0:
                    sigma = cand
            except (TypeError, ValueError):
                pass
        return baseline, max(sigma, 1e-3)

    @staticmethod
    def _z_score(
        value: float, baseline: float, sigma: float, direction: _Direction,
    ) -> float:
        raw = (value - baseline) / sigma
        # Direction gates which side of the distribution counts as
        # "anomalous". A blink rate of 0 below a baseline of 17 is
        # interesting in "abs" mode but uninteresting for a signal where
        # only high values matter (e.g. tab thrashing). Return 0 for the
        # wrong side so the |z| floor in ``attribute_top_signals``
        # filters the noise out.
        if direction == "below":
            return -raw if raw < 0 else 0.0
        if direction == "above":
            return raw if raw > 0 else 0.0
        return abs(raw)


def attribute_top_signals(
    features: object,
    baselines: UserBaselines | None = None,
    *,
    max_signals: int = 3,
    sample_buffers: Iterable[tuple[str, Iterable[float]]] | None = None,
) -> list[CausalSignal]:
    """Convenience one-shot used by tests; mirrors the planner-side call.

    ``sample_buffers`` is an optional iterable of ``(key, samples)``
    pairs so tests can inject pre-populated sparkline data without
    instantiating a long-running :class:`CausalAttributor`.
    """
    attributor = CausalAttributor()
    if sample_buffers:
        for key, samples in sample_buffers:
            if key in attributor._buffers:
                for v in samples:
                    attributor._buffers[key].append(float(v))
    return attributor.attribute_top_signals(
        features, baselines, max_signals=max_signals,
    )


__all__ = [
    "CausalAttributor",
    "attribute_top_signals",
]
