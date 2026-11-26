"""Personal reference levels for camera physiology.

Camera physiology is only interpretable against the same person measured by
the same camera: absolute heart-rate variability from a webcam carries a
device- and lighting-dependent bias, but a *change* against one's own recent
level largely cancels it.  The tracker turns the stream of published values
into a per-metric reference (centre and spread) that the scorer standardises
against.

A reference becomes ready in one of two ways:

* a calibration profile already holds a measured level for the metric, or
* the session itself has supplied enough valid observations to estimate one.

After that it adapts slowly, with updates clipped to a few spreads, so a long
stressful stretch reads as a deviation instead of quietly becoming the new
normal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Literal

import numpy as np

from cortex.libs.schemas.state import UserBaselines


class PhysiologyMetricName(StrEnum):
    HEART_RATE = "heart_rate_bpm"
    SDNN = "hrv_sdnn_ms"
    RESPIRATION = "respiration_rate_bpm"
    BLINK_RATE = "blink_rate_per_min"


ReferenceSource = Literal["calibration", "session", "unavailable"]


@dataclass(frozen=True, slots=True)
class MetricReference:
    """Centre and spread a measurement is standardised against."""

    center: float
    spread: float
    source: ReferenceSource
    ready: bool

    def z(self, value: float) -> float:
        """Standard score of ``value``; 0.0 while the reference is not ready."""

        if not self.ready or self.spread <= 0.0:
            return 0.0
        return (value - self.center) / self.spread


_UNAVAILABLE = MetricReference(center=0.0, spread=1.0, source="unavailable", ready=False)


@dataclass(frozen=True, slots=True)
class PhysiologyReference:
    """One immutable snapshot of every metric's reference."""

    heart_rate: MetricReference = _UNAVAILABLE
    sdnn: MetricReference = _UNAVAILABLE
    respiration: MetricReference = _UNAVAILABLE
    blink_rate: MetricReference = _UNAVAILABLE

    def for_metric(self, metric: PhysiologyMetricName) -> MetricReference:
        return {
            PhysiologyMetricName.HEART_RATE: self.heart_rate,
            PhysiologyMetricName.SDNN: self.sdnn,
            PhysiologyMetricName.RESPIRATION: self.respiration,
            PhysiologyMetricName.BLINK_RATE: self.blink_rate,
        }[metric]


# Smallest spread each metric may be standardised against.  Without a floor a
# very steady warm-up would turn ordinary fluctuation into large z-scores.
_ABSOLUTE_SPREAD_FLOOR = {
    PhysiologyMetricName.HEART_RATE: 3.0,
    PhysiologyMetricName.SDNN: 6.0,
    PhysiologyMetricName.RESPIRATION: 1.5,
    PhysiologyMetricName.BLINK_RATE: 3.0,
}
_RELATIVE_SPREAD_FLOOR = {
    PhysiologyMetricName.HEART_RATE: 0.05,
    PhysiologyMetricName.SDNN: 0.20,
    PhysiologyMetricName.RESPIRATION: 0.12,
    PhysiologyMetricName.BLINK_RATE: 0.25,
}
_PLAUSIBLE_RANGE = {
    PhysiologyMetricName.HEART_RATE: (30.0, 220.0),
    PhysiologyMetricName.SDNN: (1.0, 300.0),
    PhysiologyMetricName.RESPIRATION: (3.0, 45.0),
    PhysiologyMetricName.BLINK_RATE: (0.0, 60.0),
}


def _floored_spread(metric: PhysiologyMetricName, center: float, spread: float) -> float:
    return max(
        spread,
        _ABSOLUTE_SPREAD_FLOOR[metric],
        _RELATIVE_SPREAD_FLOOR[metric] * abs(center),
    )


class _MetricState:
    """Warm-up sample store followed by a clipped exponential tracker."""

    def __init__(
        self,
        metric: PhysiologyMetricName,
        *,
        warmup_seconds: float,
        warmup_samples: int,
        half_life_seconds: float,
    ) -> None:
        self.metric = metric
        self._warmup_seconds = warmup_seconds
        self._warmup_samples = warmup_samples
        self._half_life_seconds = half_life_seconds
        self._samples: list[float] = []
        self._first_at: float | None = None
        self._last_at: float | None = None
        self._center: float | None = None
        self._spread: float | None = None
        self._source: ReferenceSource = "unavailable"

    def seed(self, center: float, spread: float) -> None:
        self._center = float(center)
        self._spread = _floored_spread(self.metric, center, spread)
        self._source = "calibration"

    def observe(self, value: float, now: float) -> None:
        low, high = _PLAUSIBLE_RANGE[self.metric]
        if not isfinite(value) or not isfinite(now) or not low <= value <= high:
            return
        if self._last_at is not None and now <= self._last_at:
            return
        elapsed = 0.0 if self._last_at is None else now - self._last_at
        self._last_at = now
        if self._center is None or self._spread is None:
            if self._first_at is None:
                self._first_at = now
            self._samples.append(float(value))
            if (
                len(self._samples) >= self._warmup_samples
                and now - self._first_at >= self._warmup_seconds
            ):
                samples = np.asarray(self._samples, dtype=np.float64)
                center = float(np.median(samples))
                mad = float(np.median(np.abs(samples - center)))
                self._center = center
                self._spread = _floored_spread(self.metric, center, 1.4826 * mad)
                self._source = "session"
                self._samples.clear()
            return
        # A gap is not evidence of stability: cap the weight one update earns.
        weight = 1.0 - 0.5 ** (min(elapsed, 30.0) / self._half_life_seconds)
        clipped = float(
            np.clip(
                value,
                self._center - 3.0 * self._spread,
                self._center + 3.0 * self._spread,
            )
        )
        deviation = abs(clipped - self._center)
        self._center += weight * (clipped - self._center)
        # 1.2533 * mean absolute deviation estimates a Gaussian sigma.
        self._spread = _floored_spread(
            self.metric,
            self._center,
            self._spread + weight * (1.2533 * deviation - self._spread),
        )

    def reference(self) -> MetricReference:
        if self._center is None or self._spread is None:
            return _UNAVAILABLE
        return MetricReference(
            center=self._center,
            spread=self._spread,
            source=self._source,
            ready=True,
        )


class PhysiologyBaselineTracker:
    """Maintain the personal reference for every camera physiology metric."""

    def __init__(
        self,
        baselines: UserBaselines | None = None,
        *,
        warmup_seconds: float = 120.0,
        warmup_samples: int = 60,
        half_life_seconds: float = 3600.0,
    ) -> None:
        if warmup_seconds < 0 or warmup_samples < 1 or half_life_seconds <= 0:
            raise ValueError("invalid physiology baseline configuration")
        self._states = {
            metric: _MetricState(
                metric,
                warmup_seconds=warmup_seconds,
                warmup_samples=warmup_samples,
                half_life_seconds=half_life_seconds,
            )
            for metric in PhysiologyMetricName
        }
        if baselines is not None:
            self.seed_from_calibration(baselines)

    def seed_from_calibration(self, baselines: UserBaselines) -> None:
        """Adopt measured calibration levels; population defaults are ignored.

        ``UserBaselines`` carries population placeholders until a calibration
        runs.  Standardising against a placeholder would compare a person
        with an average nobody measured, so only metrics present in the
        profile's measured distributions seed a reference.
        """

        if not baselines.is_calibrated:
            return
        distributions = baselines.metric_distributions
        for metric, keys in (
            (PhysiologyMetricName.HEART_RATE, ("heart_rate_bpm", "hr")),
            (PhysiologyMetricName.SDNN, ("hrv_sdnn_ms", "hrv_sdnn")),
            (
                PhysiologyMetricName.RESPIRATION,
                ("respiration_rate_bpm", "respiration_rate", "resp_rate"),
            ),
            (PhysiologyMetricName.BLINK_RATE, ("blink_rate_per_min", "blink_rate")),
        ):
            for key in keys:
                stats = distributions.get(key)
                if not stats:
                    continue
                center = stats.get("mu", stats.get("mean"))
                spread = stats.get("sigma", stats.get("std"))
                if center is None or not isfinite(float(center)):
                    continue
                self._states[metric].seed(
                    float(center),
                    float(spread) if spread is not None and isfinite(float(spread)) else 0.0,
                )
                break

    def observe(
        self,
        metric: PhysiologyMetricName,
        value: float | None,
        now: float,
    ) -> None:
        """Offer one valid published value; ``None`` is ignored."""

        if value is None:
            return
        self._states[metric].observe(float(value), float(now))

    def reference(self) -> PhysiologyReference:
        return PhysiologyReference(
            heart_rate=self._states[PhysiologyMetricName.HEART_RATE].reference(),
            sdnn=self._states[PhysiologyMetricName.SDNN].reference(),
            respiration=self._states[PhysiologyMetricName.RESPIRATION].reference(),
            blink_rate=self._states[PhysiologyMetricName.BLINK_RATE].reference(),
        )
