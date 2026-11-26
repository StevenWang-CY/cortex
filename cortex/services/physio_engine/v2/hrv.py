"""Metric-specific pulse-rate-variability estimates over the beat ledger.

Every metric has its own evidence contract: how much recent beat coverage it
needs, how many accepted intervals, and how much artifact it tolerates.  A
metric that misses its contract is published as unavailable with the reason,
never as a number.

Three properties of camera-derived beats shape the implementation:

* **The analysis window trails the newest beat.**  The ledger keeps ten
  minutes of history; a statistic over all of it would answer "how variable
  was the last ten minutes" and lag every change by minutes.  Each metric is
  computed over its own trailing window instead.
* **Successive differences only span adjacent beats.**  When an interval is
  rejected (a missed or spurious peak), the two accepted intervals around it
  are not successive.  Differencing them anyway counts the artifact twice.
* **Peak-time jitter inflates beat-to-beat metrics.**  A camera samples the
  pulse wave at 30 Hz through sensor noise, so each beat instant carries a
  timing error that RMSSD, SD1 and pNN50 read as variability.  The
  qualification harness quantifies that bias; the values are meaningful as a
  change against a personal baseline taken with the same camera, which is how
  the support engine uses them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import trapezoid
from scipy.signal import lombscargle

from cortex.libs.schemas.physiology import (
    BeatStatus,
    EstimateUncertainty,
    EvidenceStatus,
    InterBeatInterval,
    PhysiologyMetric,
    SignalAlgorithmIdentity,
    SignalEstimate,
)
from cortex.services.physio_engine.v2.beat_timing import timing_noise_correlation
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.provenance import (
    code_sha256,
    configuration_sha256,
)

# Trailing analysis windows (seconds).  Beat-to-beat metrics use two minutes;
# dispersion and spectral metrics three, long enough to hold several cycles
# of the slow (0.04-0.15 Hz) interval swings and short enough to follow a
# change of state within a few minutes.
BEAT_TO_BEAT_WINDOW_SECONDS = 120.0
DISPERSION_WINDOW_SECONDS = 180.0
# An accepted interval further than this from the window median is treated as
# a missed or doubled beat (the usual 20-30% quotient filter).
MEDIAN_DEVIATION_LIMIT = 0.30
# Timing noise is removed from the statistics in variance.  When it accounts
# for more than this share of what was measured, the remainder is a small
# difference of two large, uncertain numbers and is not published.
MAX_TIMING_NOISE_SHARE = 0.85
# A corrected variance is never reported below this share of the measured
# one: an over-estimated noise term must not manufacture a collapse.
MIN_CORRECTED_VARIANCE_SHARE = 0.05
# Fraction of the window's intervals that must carry a timing-noise estimate
# before the correction is applied at all.
MIN_TIMING_ESTIMATE_COVERAGE = 0.80

_UNITS: dict[PhysiologyMetric, str] = {
    PhysiologyMetric.RMSSD: "ms",
    PhysiologyMetric.SDNN: "ms",
    PhysiologyMetric.PNN50: "ratio",
    PhysiologyMetric.SD1: "ms",
    PhysiologyMetric.SD2: "ms",
    PhysiologyMetric.LF_HF_RATIO: "ratio",
    PhysiologyMetric.SAMPLE_ENTROPY: "dimensionless",
}


@dataclass(frozen=True)
class IntervalSeries:
    """Accepted intervals of one trailing window, with adjacency preserved."""

    durations_ms: NDArray[np.float64]
    end_times_s: NDArray[np.float64]
    # ``successive[i]`` is True when interval ``i`` directly follows ``i-1``.
    successive: NDArray[np.bool_]
    span_seconds: float
    artifact_fraction: float
    quality: float
    start_mono_ns: int
    end_mono_ns: int
    # Variance (ms^2) that beat-timing noise adds to one interval and to one
    # successive difference; zero when no estimate is available.
    interval_noise_variance: float = 0.0
    difference_noise_variance: float = 0.0
    timing_sigma_ms: float | None = None

    @property
    def count(self) -> int:
        return int(len(self.durations_ms))

    def timing_noise_share(self) -> float:
        """Share of the measured successive-difference power that is noise."""

        differences = self.successive_differences()
        if len(differences) < 2 or self.difference_noise_variance <= 0.0:
            return 0.0
        measured = float(np.mean(differences**2))
        if measured <= 1e-12:
            return 1.0
        return float(min(1.0, self.difference_noise_variance / measured))

    def successive_differences(self) -> NDArray[np.float64]:
        if self.count < 2:
            return np.asarray([], dtype=np.float64)
        differences = np.diff(self.durations_ms)
        return np.asarray(differences[self.successive[1:]], dtype=np.float64)


def _timing_noise_terms(
    kept: list[InterBeatInterval],
    durations_ms: NDArray[np.float64],
) -> tuple[float, float, float | None]:
    """Noise variance added to an interval and to a successive difference.

    With per-beat timing error ``e`` of variance ``s2`` and correlation
    ``r1``/``r2`` at one and two beats' lag, an interval ``e[n+1] - e[n]``
    carries ``2 * s2 * (1 - r1)`` and a successive difference
    ``e[n+2] - 2 e[n+1] + e[n]`` carries ``s2 * (6 - 8 r1 + 2 r2)``.
    """

    estimates = np.asarray(
        [item.timing_sigma_ms for item in kept if item.timing_sigma_ms is not None],
        dtype=np.float64,
    )
    if len(kept) == 0 or len(estimates) < MIN_TIMING_ESTIMATE_COVERAGE * len(kept):
        return 0.0, 0.0, None
    variance = float(np.mean(estimates**2))
    mean_interval_s = float(np.mean(durations_ms)) / 1_000.0
    if mean_interval_s <= 0.0:
        return 0.0, 0.0, None
    cardiac_hz = 1.0 / mean_interval_s
    r1 = timing_noise_correlation(mean_interval_s, cardiac_hz=cardiac_hz)
    r2 = timing_noise_correlation(2.0 * mean_interval_s, cardiac_hz=cardiac_hz)
    return (
        max(0.0, 2.0 * variance * (1.0 - r1)),
        max(0.0, variance * (6.0 - 8.0 * r1 + 2.0 * r2)),
        float(np.sqrt(variance)),
    )


def _corrected(measured_variance: float, noise_variance: float) -> float:
    """Remove a noise variance, never below a floor share of the measurement."""

    return max(
        measured_variance - noise_variance,
        MIN_CORRECTED_VARIANCE_SHARE * measured_variance,
        0.0,
    )


def select_interval_series(
    intervals: tuple[InterBeatInterval, ...],
    *,
    window_seconds: float,
) -> IntervalSeries:
    """Return the accepted, median-consistent intervals of the trailing window."""

    if not intervals:
        return IntervalSeries(
            durations_ms=np.asarray([], dtype=np.float64),
            end_times_s=np.asarray([], dtype=np.float64),
            successive=np.asarray([], dtype=np.bool_),
            span_seconds=0.0,
            artifact_fraction=1.0,
            quality=0.0,
            start_mono_ns=0,
            end_mono_ns=0,
        )
    latest_ns = intervals[-1].end_mono_ns
    cutoff_ns = latest_ns - int(window_seconds * 1_000_000_000)
    windowed = [item for item in intervals if item.start_mono_ns >= cutoff_ns]
    accepted_flags = [item.status == BeatStatus.ACCEPTED.value for item in windowed]
    accepted_durations = np.asarray(
        [item.duration_ms for item, ok in zip(windowed, accepted_flags, strict=True) if ok],
        dtype=np.float64,
    )
    if len(accepted_durations) >= 5:
        median = float(np.median(accepted_durations))
        accepted_flags = [
            ok and abs(item.duration_ms - median) <= MEDIAN_DEVIATION_LIMIT * median
            for item, ok in zip(windowed, accepted_flags, strict=True)
        ]
    kept = [item for item, ok in zip(windowed, accepted_flags, strict=True) if ok]
    if not kept:
        return IntervalSeries(
            durations_ms=np.asarray([], dtype=np.float64),
            end_times_s=np.asarray([], dtype=np.float64),
            successive=np.asarray([], dtype=np.bool_),
            span_seconds=0.0,
            artifact_fraction=1.0,
            quality=0.0,
            start_mono_ns=windowed[0].start_mono_ns if windowed else 0,
            end_mono_ns=latest_ns,
        )
    # Interval i follows interval i-1 exactly when they share a beat.
    successive = np.zeros(len(kept), dtype=np.bool_)
    for index in range(1, len(kept)):
        successive[index] = kept[index].left_beat_id == kept[index - 1].right_beat_id
    covered_ns = sum(item.end_mono_ns - item.start_mono_ns for item in kept)
    durations = np.asarray([item.duration_ms for item in kept], dtype=np.float64)
    interval_noise, difference_noise, sigma_ms = _timing_noise_terms(kept, durations)
    return IntervalSeries(
        interval_noise_variance=interval_noise,
        difference_noise_variance=difference_noise,
        timing_sigma_ms=sigma_ms,
        durations_ms=np.asarray([item.duration_ms for item in kept], dtype=np.float64),
        end_times_s=np.asarray(
            [item.end_mono_ns / 1_000_000_000.0 for item in kept], dtype=np.float64
        ),
        successive=successive,
        # Coverage is the time the accepted intervals actually span, not the
        # distance between the first and last beat: a window that is mostly
        # rejected beats must not look long.
        span_seconds=covered_ns / 1_000_000_000.0,
        artifact_fraction=1.0 - len(kept) / len(windowed),
        quality=float(np.mean([item.quality for item in kept])),
        start_mono_ns=kept[0].start_mono_ns,
        end_mono_ns=kept[-1].end_mono_ns,
    )


def rmssd_ms(series: IntervalSeries) -> float | None:
    differences = series.successive_differences()
    if len(differences) < 2:
        return None
    measured = float(np.mean(differences**2))
    return float(np.sqrt(_corrected(measured, series.difference_noise_variance)))


def sdnn_ms(series: IntervalSeries) -> float | None:
    """Standard deviation of the intervals, uncorrected.

    Dispersion is dominated by the slow swings of the interval series, which
    beat-timing noise barely touches: against ECG on the 4TU reference videos
    the raw statistic is within 7 ms on average with a bias under 2 ms,
    while subtracting the modelled timing noise over-corrects it by 7 ms.
    """

    if series.count < 3:
        return None
    return float(np.std(series.durations_ms, ddof=1))


def pnn50(series: IntervalSeries) -> float | None:
    differences = series.successive_differences()
    if len(differences) < 2:
        return None
    return float(np.mean(np.abs(differences) > 50.0))


def sd1_ms(series: IntervalSeries) -> float | None:
    differences = series.successive_differences()
    if len(differences) < 3:
        return None
    measured = float(np.var(differences, ddof=1))
    return float(np.sqrt(_corrected(measured, series.difference_noise_variance) / 2.0))


def sd2_ms(series: IntervalSeries) -> float | None:
    differences = series.successive_differences()
    dispersion = sdnn_ms(series)
    if dispersion is None or len(differences) < 3:
        return None
    difference_variance = _corrected(
        float(np.var(differences, ddof=1)), series.difference_noise_variance
    )
    variance = 2.0 * dispersion**2 - 0.5 * difference_variance
    return float(np.sqrt(max(0.0, variance)))


def lf_hf_ratio(series: IntervalSeries) -> float | None:
    """LF (0.04-0.15 Hz) over HF (0.15-0.40 Hz) power by Lomb-Scargle.

    The periodogram runs on the unevenly spaced beat instants directly, so no
    resampling or gap interpolation is needed.
    """

    if series.count < 30:
        return None
    centred = series.durations_ms - float(np.mean(series.durations_ms))
    if float(np.std(centred)) <= 1e-9:
        return None
    times = series.end_times_s - float(series.end_times_s[0])
    frequencies = np.linspace(0.04, 0.40, 256)
    power = lombscargle(times, centred, 2.0 * np.pi * frequencies, normalize=False)
    low = frequencies < 0.15
    lf = float(trapezoid(power[low], frequencies[low]))
    hf = float(trapezoid(power[~low], frequencies[~low]))
    if hf <= 1e-12:
        return None
    return float(lf / hf)


def sample_entropy(series: IntervalSeries, *, m: int = 2, r_ratio: float = 0.2) -> float | None:
    """Sample entropy SampEn(m, r) of the interval series (vectorised)."""

    values = series.durations_ms
    count = len(values)
    if count < 60:
        return None
    spread = float(np.std(values))
    if spread <= 1e-9:
        return None
    tolerance = r_ratio * spread

    def _matches(order: int) -> int:
        templates = np.lib.stride_tricks.sliding_window_view(values, order)[
            : count - m
        ]
        distance = np.max(
            np.abs(templates[:, None, :] - templates[None, :, :]), axis=2
        )
        return int((np.sum(distance <= tolerance) - len(templates)) // 2)

    shorter = _matches(m)
    longer = _matches(m + 1)
    if shorter <= 0 or longer <= 0:
        return None
    return float(-np.log(longer / shorter))


def _moving_block_interval(
    series: IntervalSeries,
    statistic: Callable[[IntervalSeries], float | None],
) -> EstimateUncertainty | None:
    """Deterministic 95% moving-block bootstrap interval over the intervals."""

    count = series.count
    if count < 8:
        return None
    block = max(2, int(round(np.sqrt(count))))
    starts = np.arange(0, max(1, count - block + 1), dtype=np.int64)
    seed_bytes = hashlib.sha256(series.durations_ms.tobytes()).digest()[:8]
    rng = np.random.default_rng(int.from_bytes(seed_bytes, "big"))
    draws: list[float] = []
    for _ in range(100):
        picks = rng.choice(starts, size=int(np.ceil(count / block)))
        index = np.concatenate([np.arange(start, start + block) for start in picks])[:count]
        successive = series.successive[index].copy()
        # A block boundary is not a pair of successive beats.
        successive[::block] = False
        resampled = IntervalSeries(
            durations_ms=series.durations_ms[index],
            end_times_s=series.end_times_s[index],
            successive=successive,
            span_seconds=series.span_seconds,
            artifact_fraction=series.artifact_fraction,
            quality=series.quality,
            start_mono_ns=series.start_mono_ns,
            end_mono_ns=series.end_mono_ns,
            interval_noise_variance=series.interval_noise_variance,
            difference_noise_variance=series.difference_noise_variance,
            timing_sigma_ms=series.timing_sigma_ms,
        )
        estimate = statistic(resampled)
        if estimate is not None and np.isfinite(estimate):
            draws.append(float(estimate))
    if len(draws) < 20:
        return None
    lower, upper = np.percentile(np.asarray(draws), [2.5, 97.5])
    return EstimateUncertainty(
        lower=float(max(0.0, lower)),
        upper=float(max(0.0, upper)),
        confidence_level=0.95,
        method="deterministic-moving-block-bootstrap-100",
    )


@dataclass(frozen=True)
class _MetricContract:
    metric: PhysiologyMetric
    statistic: Callable[[IntervalSeries], float | None]
    window_seconds: float
    minimum_seconds: float
    minimum_intervals: int
    bootstrap: bool = False
    # Beat-to-beat metrics are the ones timing noise can swamp.
    noise_limited: bool = False


def build_hrv_estimates(
    intervals: tuple[InterBeatInterval, ...],
    *,
    algorithm: SignalAlgorithmIdentity,
    boot_id: UUID,
    enabled: bool,
    rmssd_min_seconds: float = 60.0,
    sdnn_min_seconds: float = 90.0,
    min_valid_ibi: int = 40,
    max_artifact_fraction: float = 0.25,
) -> dict[PhysiologyMetric, SignalEstimate]:
    """Return every HRV metric; each is unavailable unless its contract holds."""

    parameters: dict[str, str | int | float | bool] = {
        "upstream_algorithm": algorithm.name,
        "upstream_version": algorithm.version,
        "upstream_implementation_sha256": algorithm.implementation_sha256,
        "rmssd_min_seconds": rmssd_min_seconds,
        "sdnn_min_seconds": sdnn_min_seconds,
        "min_valid_ibi": min_valid_ibi,
        "max_artifact_fraction": max_artifact_fraction,
        "beat_to_beat_window_seconds": BEAT_TO_BEAT_WINDOW_SECONDS,
        "dispersion_window_seconds": DISPERSION_WINDOW_SECONDS,
        "median_deviation_limit": MEDIAN_DEVIATION_LIMIT,
        "max_timing_noise_share": MAX_TIMING_NOISE_SHARE,
        "min_corrected_variance_share": MIN_CORRECTED_VARIANCE_SHARE,
        "publication_enabled": enabled,
    }
    if algorithm.configuration_sha256 is not None:
        parameters["upstream_configuration_sha256"] = algorithm.configuration_sha256
    hrv_algorithm = SignalAlgorithmIdentity(
        name=f"hrv-v2:{algorithm.name}",
        version="hrv-v2/3.1.0",
        implementation_sha256=code_sha256(
            (
                build_hrv_estimates,
                select_interval_series,
                _timing_noise_terms,
                _corrected,
                timing_noise_correlation,
                _moving_block_interval,
                rmssd_ms,
                sdnn_ms,
                pnn50,
                sd1_ms,
                sd2_ms,
                lf_hf_ratio,
                sample_entropy,
                BeatLedger._derive_intervals,
            ),
            dependency_sha256=(algorithm.implementation_sha256,),
        ),
        asset_sha256=algorithm.asset_sha256,
        configuration_sha256=configuration_sha256(parameters),
        parameters=parameters,
        selection_mode=algorithm.selection_mode,
    )

    dispersion_seconds = max(sdnn_min_seconds, rmssd_min_seconds)
    contracts = (
        _MetricContract(
            PhysiologyMetric.RMSSD, rmssd_ms, BEAT_TO_BEAT_WINDOW_SECONDS,
            rmssd_min_seconds, min_valid_ibi, bootstrap=True, noise_limited=True,
        ),
        _MetricContract(
            PhysiologyMetric.PNN50, pnn50, BEAT_TO_BEAT_WINDOW_SECONDS,
            rmssd_min_seconds, min_valid_ibi, noise_limited=True,
        ),
        _MetricContract(
            PhysiologyMetric.SD1, sd1_ms, BEAT_TO_BEAT_WINDOW_SECONDS,
            rmssd_min_seconds, min_valid_ibi, noise_limited=True,
        ),
        _MetricContract(
            PhysiologyMetric.SDNN, sdnn_ms, DISPERSION_WINDOW_SECONDS,
            dispersion_seconds, 2 * min_valid_ibi, bootstrap=True,
        ),
        _MetricContract(
            PhysiologyMetric.SD2, sd2_ms, DISPERSION_WINDOW_SECONDS,
            dispersion_seconds, 2 * min_valid_ibi,
        ),
        # Six cycles of the slowest LF component (0.04 Hz) take 150 s.
        _MetricContract(
            PhysiologyMetric.LF_HF_RATIO, lf_hf_ratio, DISPERSION_WINDOW_SECONDS,
            max(150.0, dispersion_seconds), 3 * min_valid_ibi,
        ),
        _MetricContract(
            PhysiologyMetric.SAMPLE_ENTROPY, sample_entropy,
            DISPERSION_WINDOW_SECONDS, dispersion_seconds, 3 * min_valid_ibi,
        ),
    )

    series_by_window: dict[float, IntervalSeries] = {}
    results: dict[PhysiologyMetric, SignalEstimate] = {}
    for contract in contracts:
        series = series_by_window.get(contract.window_seconds)
        if series is None:
            series = select_interval_series(
                intervals, window_seconds=contract.window_seconds
            )
            series_by_window[contract.window_seconds] = series

        reason: str | None = None
        if not enabled:
            reason = "pulse-rate variability is turned off in settings"
        elif series.count < contract.minimum_intervals:
            reason = (
                f"needs {contract.minimum_intervals} clean beat intervals "
                f"(have {series.count})"
            )
        elif series.span_seconds < contract.minimum_seconds:
            reason = (
                f"needs {contract.minimum_seconds:.0f} s of clean beats "
                f"(have {series.span_seconds:.0f} s)"
            )
        elif series.artifact_fraction > max_artifact_fraction:
            reason = "too many rejected beats in the analysis window"
        elif (
            contract.noise_limited
            and series.timing_noise_share() > MAX_TIMING_NOISE_SHARE
        ):
            reason = "pulse signal too noisy for beat-to-beat variability"
        value = contract.statistic(series) if reason is None else None
        if reason is None and (value is None or not np.isfinite(value)):
            reason = "not enough successive beats to compute this metric"
            value = None

        unit = _UNITS[contract.metric]
        if value is None:
            results[contract.metric] = SignalEstimate(
                metric=contract.metric,
                value=None,
                unit=unit,
                status=EvidenceStatus.UNAVAILABLE,
                quality=series.quality,
                algorithm=hrv_algorithm,
                unavailable_reason=reason,
                window_start_mono_ns=series.start_mono_ns,
                window_end_mono_ns=series.end_mono_ns,
                boot_id=boot_id,
            )
            continue
        uncertainty = (
            _moving_block_interval(series, contract.statistic)
            if contract.bootstrap
            else None
        )
        if uncertainty is not None and not (
            uncertainty.lower <= value <= uncertainty.upper
        ):
            # A bootstrap interval that excludes its own point estimate says
            # the resampling is unstable, not that the estimate is elsewhere.
            uncertainty = None
        results[contract.metric] = SignalEstimate(
            metric=contract.metric,
            value=float(value),
            unit=unit,
            status=EvidenceStatus.EXPERIMENTAL,
            quality=float(
                np.clip(
                    series.quality
                    * (1.0 - series.timing_noise_share() if contract.noise_limited else 1.0),
                    0.0,
                    1.0,
                )
            ),
            algorithm=hrv_algorithm,
            uncertainty=uncertainty,
            window_start_mono_ns=series.start_mono_ns,
            window_end_mono_ns=series.end_mono_ns,
            boot_id=boot_id,
        )
    return results
