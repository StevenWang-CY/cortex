"""Metric-specific HRV readiness over the canonical beat ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from cortex.libs.schemas.physiology import (
    BeatStatus,
    EstimateUncertainty,
    EvidenceStatus,
    InterBeatInterval,
    PhysiologyMetric,
    SignalAlgorithmIdentity,
    SignalEstimate,
)
from cortex.libs.signal.peak_detection import compute_rmssd, compute_sdnn
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.provenance import (
    code_sha256,
    configuration_sha256,
)


def _unavailable(
    metric: PhysiologyMetric,
    *,
    unit: str,
    reason: str,
    algorithm: SignalAlgorithmIdentity,
    quality: float,
    window_start_mono_ns: int,
    window_end_mono_ns: int,
    boot_id: UUID,
) -> SignalEstimate:
    return SignalEstimate(
        metric=metric,
        value=None,
        unit=unit,
        status=EvidenceStatus.UNAVAILABLE,
        quality=quality,
        algorithm=algorithm,
        unavailable_reason=reason,
        window_start_mono_ns=window_start_mono_ns,
        window_end_mono_ns=window_end_mono_ns,
        boot_id=boot_id,
    )


def _moving_block_interval(
    values: NDArray[np.float64],
    statistic: Callable[[NDArray[np.float64]], float | None],
) -> EstimateUncertainty | None:
    """Deterministic 95% moving-block bootstrap interval."""

    n = len(values)
    if n < 8:
        return None
    block = max(2, int(round(np.sqrt(n))))
    starts = np.arange(0, max(1, n - block + 1), dtype=np.int64)
    seed_bytes = hashlib.sha256(values.tobytes()).digest()[:8]
    rng = np.random.default_rng(int.from_bytes(seed_bytes, "big"))
    draws: list[float] = []
    for _ in range(200):
        sampled: list[float] = []
        while len(sampled) < n:
            start = int(rng.choice(starts))
            sampled.extend(values[start : start + block].tolist())
        estimate = statistic(np.asarray(sampled[:n], dtype=np.float64))
        if estimate is not None and np.isfinite(estimate):
            draws.append(float(estimate))
    if len(draws) < 20:
        return None
    lower, upper = np.percentile(np.asarray(draws), [2.5, 97.5])
    return EstimateUncertainty(
        lower=float(max(0.0, lower)),
        upper=float(max(0.0, upper)),
        confidence_level=0.95,
        method="deterministic-moving-block-bootstrap-200",
    )


def build_hrv_estimates(
    intervals: tuple[InterBeatInterval, ...],
    *,
    algorithm: SignalAlgorithmIdentity,
    boot_id: UUID,
    enabled: bool,
    rmssd_min_seconds: float = 180.0,
    sdnn_min_seconds: float = 300.0,
    min_valid_ibi: int = 120,
    max_artifact_fraction: float = 0.10,
) -> dict[PhysiologyMetric, SignalEstimate]:
    """Return all HRV metrics, explicitly unavailable unless each gate passes."""

    parameters: dict[str, str | int | float | bool] = {
        "upstream_algorithm": algorithm.name,
        "upstream_version": algorithm.version,
        "upstream_implementation_sha256": algorithm.implementation_sha256,
        "rmssd_min_seconds": rmssd_min_seconds,
        "sdnn_min_seconds": sdnn_min_seconds,
        "min_valid_ibi": min_valid_ibi,
        "max_artifact_fraction": max_artifact_fraction,
        "experimental_publication_enabled": enabled,
    }
    if algorithm.configuration_sha256 is not None:
        parameters["upstream_configuration_sha256"] = (
            algorithm.configuration_sha256
        )
    hrv_algorithm = SignalAlgorithmIdentity(
        name=f"hrv-v2:{algorithm.name}",
        version="hrv-v2/2.0.0",
        implementation_sha256=code_sha256(
            (
                build_hrv_estimates,
                _moving_block_interval,
                compute_rmssd,
                compute_sdnn,
                BeatLedger._derive_intervals,
            ),
            dependency_sha256=(algorithm.implementation_sha256,),
        ),
        asset_sha256=algorithm.asset_sha256,
        configuration_sha256=configuration_sha256(parameters),
        parameters=parameters,
        selection_mode=algorithm.selection_mode,
    )

    if intervals:
        start_ns = intervals[0].start_mono_ns
        end_ns = intervals[-1].end_mono_ns
    else:
        start_ns = end_ns = 0
    accepted = [item for item in intervals if item.status == BeatStatus.ACCEPTED.value]
    values = np.asarray([item.duration_ms for item in accepted], dtype=np.float64)
    artifact_fraction = (
        1.0 - len(accepted) / len(intervals) if intervals else 1.0
    )
    quality = float(np.mean([item.quality for item in accepted])) if accepted else 0.0
    duration_s = (end_ns - start_ns) / 1_000_000_000.0

    results: dict[PhysiologyMetric, SignalEstimate] = {}
    unsupported = {
        PhysiologyMetric.PNN50: "requires a separate reference-validation gate",
        PhysiologyMetric.SD1: "nonlinear HRV is disabled pending validation",
        PhysiologyMetric.SD2: "nonlinear HRV is disabled pending validation",
        PhysiologyMetric.LF_HF_RATIO: "frequency HRV is disabled pending stationarity and respiration validation",
        PhysiologyMetric.SAMPLE_ENTROPY: "nonlinear HRV is disabled pending validation",
    }
    for metric, unavailable_reason in unsupported.items():
        unit = "ratio" if metric in {PhysiologyMetric.PNN50, PhysiologyMetric.LF_HF_RATIO} else "ms"
        if metric is PhysiologyMetric.SAMPLE_ENTROPY:
            unit = "dimensionless"
        results[metric] = _unavailable(
            metric,
            unit=unit,
            reason=unavailable_reason,
            algorithm=hrv_algorithm,
            quality=quality,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boot_id=boot_id,
        )

    for metric, minimum_s, statistic in (
        (PhysiologyMetric.RMSSD, rmssd_min_seconds, compute_rmssd),
        (PhysiologyMetric.SDNN, sdnn_min_seconds, compute_sdnn),
    ):
        reason: str | None = None
        if not enabled:
            reason = "disabled pending simultaneous ECG reference validation"
        elif len(accepted) < min_valid_ibi:
            reason = f"requires at least {min_valid_ibi} accepted intervals"
        elif duration_s < minimum_s:
            reason = f"requires at least {minimum_s:.0f} seconds of accepted beat coverage"
        elif artifact_fraction > max_artifact_fraction:
            reason = "artifact burden exceeds the metric evidence contract"
        value = statistic(values) if reason is None else None
        if reason is not None or value is None:
            results[metric] = _unavailable(
                metric,
                unit="ms",
                reason=reason or "metric computation failed",
                algorithm=hrv_algorithm,
                quality=quality,
                window_start_mono_ns=start_ns,
                window_end_mono_ns=end_ns,
                boot_id=boot_id,
            )
        else:
            results[metric] = SignalEstimate(
                metric=metric,
                value=float(value),
                unit="ms",
                status=EvidenceStatus.EXPERIMENTAL,
                quality=quality,
                algorithm=hrv_algorithm,
                uncertainty=_moving_block_interval(values, statistic),
                unavailable_reason=None,
                window_start_mono_ns=start_ns,
                window_end_mono_ns=end_ns,
                boot_id=boot_id,
            )
    return results
