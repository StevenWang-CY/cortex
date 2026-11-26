"""Qualification of camera physiology against known truth.

A metric is *qualified* for use in support inference when the production
estimator, run exactly as the app runs it, meets that metric's acceptance
criteria on evidence with known truth.  This module replays a recorded or
synthetic camera trace through :class:`CameraPhysiologyProcessor` — the same
object the runtime daemon drives — and scores what it publishes.

Two things are measured for every metric and neither is optional:

* **error when it publishes** (bias, MAE, limits of agreement, correlation),
* **how often it publishes** (coverage), including how often it publishes
  on input that contains nothing to measure.

An estimator that is accurate on the few windows it accepts and silent on the
rest is not useful; one that always answers is not trustworthy.  Reporting
both keeps the trade visible.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import CortexConfig
from cortex.libs.schemas.observations import MissingReason, ObservationValidity
from cortex.libs.schemas.physiology import PhysiologyMetric, SignalEstimate
from cortex.services.capture_service.observation_buffer import NumericObservation
from cortex.services.physio_engine.camera_physiology import (
    CameraPhysiologyProcessor,
    PhysiologyUpdate,
    build_rgb_observation_buffer,
)
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2

METRIC_HEART_RATE = "heart_rate_bpm"
METRIC_RMSSD = "rmssd_ms"
METRIC_SDNN = "sdnn_ms"
METRIC_RESPIRATION = "respiration_rate_bpm"


@dataclass(frozen=True)
class WindowedValue:
    """One published estimate and the interval it describes (seconds)."""

    value: float
    start_s: float
    end_s: float
    quality: float


@dataclass(frozen=True)
class StrideRecord:
    """What one engine stride published; ``None`` means it abstained."""

    at_s: float
    heart_rate: WindowedValue | None
    rmssd: WindowedValue | None
    sdnn: WindowedValue | None
    respiration: WindowedValue | None
    extra: dict[str, float] = field(default_factory=dict)


def _windowed(estimate: SignalEstimate | None, origin_ns: int) -> WindowedValue | None:
    if estimate is None or estimate.value is None:
        return None
    return WindowedValue(
        value=float(estimate.value),
        start_s=(estimate.window_start_mono_ns - origin_ns) / 1e9,
        end_s=(estimate.window_end_mono_ns - origin_ns) / 1e9,
        quality=float(estimate.quality),
    )


def replay_trace(
    sample_times_s: NDArray[np.float64],
    rgb: NDArray[np.float64],
    head_vertical_face_units: NDArray[np.float64] | None,
    *,
    config: CortexConfig | None = None,
    observation_quality: float | NDArray[np.float64] = 1.0,
    valid_mask: NDArray[np.bool_] | None = None,
    motion_face_widths_per_second: NDArray[np.float64] | None = None,
    boot_id: UUID | None = None,
    on_update: Callable[[float, PhysiologyUpdate], None] | None = None,
) -> list[StrideRecord]:
    """Feed a trace through the production processor at the production stride.

    ``on_update`` receives every stride's full processor output with its
    capture time, for callers that drive later stages (feature fusion, the
    rules) from the same replay.

    ``sample_times_s`` are the capture instants of each row of ``rgb``.  A
    ``False`` entry in ``valid_mask`` is delivered as a rejected observation
    (as the capture service does for a missing face or a failed quality
    gate), so window readiness and interpolation behave as in the app.
    """

    times = np.asarray(sample_times_s, dtype=np.float64)
    values = np.asarray(rgb, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or len(values) != len(times):
        raise ValueError("rgb must be [samples, 3] aligned with sample_times_s")
    if len(times) < 2 or bool((np.diff(times) <= 0).any()):
        raise ValueError("sample_times_s must be strictly increasing")
    cfg = config or CortexConfig()
    rppg = cfg.signal.rppg
    processor = CameraPhysiologyProcessor(
        rppg,
        nominal_fps=float(cfg.capture.fps),
        engine=PhysiologyEngineV2(
            rppg,
            max_motion_face_widths_per_second=(
                cfg.capture.max_motion_face_widths_per_second
            ),
        ),
    )
    buffer = build_rgb_observation_buffer(
        rppg, minimum_items=cfg.capture.observation_buffer_max_items
    )
    boot = boot_id or uuid4()
    per_sample_quality: NDArray[np.float64] | None = None
    constant_quality = 1.0
    if isinstance(observation_quality, (int, float)):
        constant_quality = float(observation_quality)
    else:
        per_sample_quality = np.asarray(observation_quality, dtype=np.float64)
    # An arbitrary positive origin: the engine only uses differences.
    origin_ns = 1_000_000_000_000
    stride_ns = int(rppg.stride_seconds * 1_000_000_000)
    last_stride_ns = 0
    records: list[StrideRecord] = []
    for index, (stamp_s, sample) in enumerate(zip(times, values, strict=True)):
        mono_ns = origin_ns + int(round(stamp_s * 1e9))
        is_valid = bool(valid_mask[index]) if valid_mask is not None else True
        is_valid = is_valid and bool(np.isfinite(sample).all())
        buffer.append(
            NumericObservation(
                observed_at_unix_ms=mono_ns // 1_000_000,
                observed_at_mono_ns=mono_ns,
                boot_id=boot,
                sequence=index,
                value=np.asarray(sample, dtype=np.float64) if is_valid else None,
                validity=(
                    ObservationValidity.VALID.value
                    if is_valid
                    else ObservationValidity.REJECTED.value
                ),
                missing_reason=None if is_valid else MissingReason.NO_FACE,
                quality=(
                    0.0
                    if not is_valid
                    else float(per_sample_quality[index])
                    if per_sample_quality is not None
                    else constant_quality
                ),
                motion_face_widths_per_second=(
                    float(motion_face_widths_per_second[index])
                    if motion_face_widths_per_second is not None
                    and is_valid
                    and np.isfinite(motion_face_widths_per_second[index])
                    else None
                ),
                head_vertical_face_units=(
                    float(head_vertical_face_units[index])
                    if head_vertical_face_units is not None
                    and is_valid
                    and np.isfinite(head_vertical_face_units[index])
                    else None
                ),
            )
        )
        if last_stride_ns and mono_ns - last_stride_ns < stride_ns:
            continue
        last_stride_ns = mono_ns
        update = processor.process(buffer.snapshot(), boot_id=boot)
        if on_update is not None:
            on_update(float(stamp_s), update)
        hrv = update.pulse.hrv_estimates if update.pulse is not None else {}
        extra: dict[str, float] = {}
        for metric in (
            PhysiologyMetric.PNN50,
            PhysiologyMetric.SD1,
            PhysiologyMetric.SD2,
            PhysiologyMetric.LF_HF_RATIO,
            PhysiologyMetric.SAMPLE_ENTROPY,
        ):
            estimate = hrv.get(metric)
            if estimate is not None and estimate.value is not None:
                extra[metric.value] = float(estimate.value)
        if update.pulse is not None:
            extra["quality"] = float(update.pulse.summary.quality)
            extra["signal_present"] = float(update.pulse.signal_present)
            for key, value in update.pulse.sqi_components.items():
                extra[f"sqi_{key}"] = float(value)
        records.append(
            StrideRecord(
                at_s=float(stamp_s),
                heart_rate=_windowed(
                    update.pulse.summary.hr if update.pulse is not None else None,
                    origin_ns,
                ),
                rmssd=_windowed(hrv.get(PhysiologyMetric.RMSSD), origin_ns),
                sdnn=_windowed(hrv.get(PhysiologyMetric.SDNN), origin_ns),
                respiration=_windowed(processor.latest_respiration, origin_ns),
                extra=extra,
            )
        )
    return records


@dataclass(frozen=True)
class MetricReport:
    """Agreement between published estimates and their truth."""

    metric: str
    attempted: int
    published: int
    coverage: float
    mae: float | None
    rmse: float | None
    bias: float | None
    p95_abs_error: float | None
    loa_lower: float | None
    loa_upper: float | None
    pearson_r: float | None
    within_tolerance: float | None
    tolerance: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_pairs(
    metric: str,
    pairs: Sequence[tuple[float, float]],
    *,
    attempted: int,
    tolerance: float,
) -> MetricReport:
    """Summarise (estimate, truth) pairs; ``attempted`` counts abstentions too."""

    published = len(pairs)
    coverage = published / attempted if attempted else 0.0
    if published == 0:
        return MetricReport(
            metric, attempted, 0, coverage, None, None, None, None, None, None,
            None, None, tolerance,
        )
    estimates = np.asarray([pair[0] for pair in pairs], dtype=np.float64)
    truth = np.asarray([pair[1] for pair in pairs], dtype=np.float64)
    error = estimates - truth
    bias = float(np.mean(error))
    spread = float(np.std(error, ddof=1)) if published > 1 else 0.0
    correlation: float | None = None
    if published >= 3 and float(np.std(estimates)) > 1e-9 and float(np.std(truth)) > 1e-9:
        correlation = float(np.corrcoef(estimates, truth)[0, 1])
    return MetricReport(
        metric=metric,
        attempted=attempted,
        published=published,
        coverage=coverage,
        mae=float(np.mean(np.abs(error))),
        rmse=float(np.sqrt(np.mean(error**2))),
        bias=bias,
        p95_abs_error=float(np.percentile(np.abs(error), 95)),
        loa_lower=bias - 1.96 * spread,
        loa_upper=bias + 1.96 * spread,
        pearson_r=correlation,
        within_tolerance=float(np.mean(np.abs(error) <= tolerance)),
        tolerance=tolerance,
    )


TruthFn = Callable[[float, float], float | None]

# What "close enough" means for the share-within-tolerance figure.  These are
# reporting conventions (5 BPM is the usual consumer heart-rate criterion),
# not acceptance gates; the gates live in ``physio_engine.qualification``.
TOLERANCES = {
    METRIC_HEART_RATE: 5.0,
    METRIC_RMSSD: 15.0,
    METRIC_SDNN: 15.0,
    METRIC_RESPIRATION: 3.0,
}


def collect_pairs(
    records: Sequence[StrideRecord],
    *,
    heart_rate_truth: TruthFn | None = None,
    rmssd_truth: TruthFn | None = None,
    sdnn_truth: TruthFn | None = None,
    respiration_truth: TruthFn | None = None,
    warmup_seconds: float = 0.0,
) -> dict[str, tuple[list[tuple[float, float]], int]]:
    """Pair every published value with truth over the interval it describes.

    Returns, per metric, the pairs and the number of strides on which truth
    existed (so an abstention counts against coverage only where a value was
    possible).  Truth is evaluated over the estimate's own window when it
    published and over the stride's trailing window when it abstained.
    """

    plans: tuple[tuple[str, str, TruthFn | None, float], ...] = (
        (METRIC_HEART_RATE, "heart_rate", heart_rate_truth, 10.0),
        (METRIC_RMSSD, "rmssd", rmssd_truth, 120.0),
        (METRIC_SDNN, "sdnn", sdnn_truth, 300.0),
        (METRIC_RESPIRATION, "respiration", respiration_truth, 45.0),
    )
    result: dict[str, tuple[list[tuple[float, float]], int]] = {}
    for name, attribute, truth_fn, nominal_window in plans:
        if truth_fn is None:
            continue
        pairs: list[tuple[float, float]] = []
        attempted = 0
        for record in records:
            if record.at_s < warmup_seconds:
                continue
            published: WindowedValue | None = getattr(record, attribute)
            if published is not None:
                truth = truth_fn(published.start_s, published.end_s)
            else:
                truth = truth_fn(max(0.0, record.at_s - nominal_window), record.at_s)
            if truth is None:
                continue
            attempted += 1
            if published is not None:
                pairs.append((published.value, float(truth)))
        result[name] = (pairs, attempted)
    return result


def merge_reports(
    collected: Sequence[dict[str, tuple[list[tuple[float, float]], int]]],
) -> dict[str, MetricReport]:
    """Pool pairs across recordings into one report per metric."""

    pooled: dict[str, tuple[list[tuple[float, float]], int]] = {}
    for item in collected:
        for name, (pairs, attempted) in item.items():
            previous_pairs, previous_attempted = pooled.get(name, ([], 0))
            pooled[name] = (previous_pairs + list(pairs), previous_attempted + attempted)
    return {
        name: score_pairs(
            name, pairs, attempted=attempted, tolerance=TOLERANCES[name]
        )
        for name, (pairs, attempted) in pooled.items()
    }


def false_publication_rate(records: Sequence[StrideRecord]) -> dict[str, float]:
    """Share of strides that published each metric on signal-free input."""

    total = len(records)
    if total == 0:
        return {}
    return {
        METRIC_HEART_RATE: sum(r.heart_rate is not None for r in records) / total,
        METRIC_RMSSD: sum(r.rmssd is not None for r in records) / total,
        METRIC_SDNN: sum(r.sdnn is not None for r in records) / total,
        METRIC_RESPIRATION: sum(r.respiration is not None for r in records) / total,
    }


@dataclass(frozen=True)
class AcceptanceCriterion:
    """What a metric must show against a reference sensor to be relied on.

    These are net-benefit thresholds for a decision-support signal that is
    read as a change against a personal reference, not clinical accuracy
    limits: the estimate must track the reference closely enough that a
    change in it means a change in the person.
    """

    metric: str
    max_mae: float
    min_within_tolerance: float
    min_correlation: float
    min_published: int = 100


ACCEPTANCE_CRITERIA: dict[str, AcceptanceCriterion] = {
    # 5 BPM is the usual consumer heart-rate tolerance.
    METRIC_HEART_RATE: AcceptanceCriterion(
        METRIC_HEART_RATE, max_mae=3.0, min_within_tolerance=0.85, min_correlation=0.90
    ),
    METRIC_SDNN: AcceptanceCriterion(
        METRIC_SDNN, max_mae=12.0, min_within_tolerance=0.70, min_correlation=0.80
    ),
    METRIC_RMSSD: AcceptanceCriterion(
        METRIC_RMSSD, max_mae=12.0, min_within_tolerance=0.70, min_correlation=0.80
    ),
    METRIC_RESPIRATION: AcceptanceCriterion(
        METRIC_RESPIRATION, max_mae=2.0, min_within_tolerance=0.80, min_correlation=0.80
    ),
}


def assess(report: MetricReport) -> tuple[bool, list[str]]:
    """Whether ``report`` meets its metric's criterion, with the reasons if not."""

    criterion = ACCEPTANCE_CRITERIA[report.metric]
    reasons: list[str] = []
    if report.published < criterion.min_published:
        reasons.append(
            f"only {report.published} published estimates (need {criterion.min_published})"
        )
    if report.mae is None or report.mae > criterion.max_mae:
        reasons.append(f"MAE {report.mae} exceeds {criterion.max_mae}")
    if (
        report.within_tolerance is None
        or report.within_tolerance < criterion.min_within_tolerance
    ):
        reasons.append(
            f"share within {report.tolerance:g} is {report.within_tolerance}, "
            f"below {criterion.min_within_tolerance}"
        )
    if report.pearson_r is None or report.pearson_r < criterion.min_correlation:
        reasons.append(
            f"correlation {report.pearson_r} below {criterion.min_correlation}"
        )
    return (not reasons, reasons)


def reports_to_json(reports: dict[str, MetricReport]) -> str:
    return json.dumps(
        {name: report.to_dict() for name, report in reports.items()},
        indent=2,
        sort_keys=True,
    )
