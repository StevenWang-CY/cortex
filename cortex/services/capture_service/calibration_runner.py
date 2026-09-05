"""Production-path, provenance-bearing calibration.

The runner owns a fresh :class:`CapturePipeline` only while calibration is in
progress and builds its estimators through the same production factory as the
runtime daemon.  Physiological rest and representative-work behavior are
separate protocol phases.  Missing data remains missing, overlapping windows
are assigned a conservative effective sample count, and simulation can only
produce a namespaced demo profile—never an active calibration.
"""

from __future__ import annotations

import asyncio
import logging
import random
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds, utc_datetime
from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.schemas.calibration import (
    CALIBRATION_FEATURE_SCHEMA_VERSION,
    CALIBRATION_PROTOCOL_VERSION,
    CalibrationBaselineValues,
    CalibrationCameraIdentity,
    CalibrationDistribution,
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationMetricSummary,
    CalibrationProfile,
    CalibrationProvenance,
    CalibrationReferenceTask,
)
from cortex.libs.schemas.observations import (
    CameraIdentity,
    MissingReason,
    ObservationValidity,
)
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.signal.angles import circular_mean_deg, wrapped_angle_delta
from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.services.capture_service.calibration_store import (
    CalibrationProfileStore,
    calibration_profile_sha256,
)

logger = logging.getLogger(__name__)


DEFAULT_DURATION_SECONDS = 120
PROGRESS_HZ = 2.0
FEATURE_SCHEMA_VERSION = CALIBRATION_FEATURE_SCHEMA_VERSION

CalibrationStatus = Literal[
    "initializing",
    "running",
    "review_required",
    "applying",
    "completed",
    "aborted",
    "failed",
]
CalibrationPhase = Literal[
    "camera_quality_check",
    "physiological_rest",
    "representative_work",
    "review",
    "commit",
]


class CalibrationCaptureUnavailable(RuntimeError):
    """Raised when live, quality-gated capture cannot be established."""


@dataclass(frozen=True)
class CalibrationProgress:
    """One honest progress snapshot for desktop and CLI transports."""

    elapsed_seconds: float
    total_seconds: float
    current_hr: float | None
    current_hrv: None
    current_sqi: float | None
    lighting_ok: bool
    motion_ok: bool
    face_ok: bool
    pct_complete: float
    status: CalibrationStatus
    phase: CalibrationPhase
    phase_instruction: str
    valid_duration_seconds: float
    missing_fraction: float


ProgressCallback = Callable[[CalibrationProgress], None]


@dataclass
class CalibrationCapture:
    """Process-local collection result; raw frames and landmarks are absent."""

    samples: dict[str, list[float]]
    camera: CameraIdentity | None
    phase_valid_duration_seconds: dict[str, float]
    phase_scheduled_count: dict[str, int]
    phase_valid_count: dict[str, int]
    phase_quality: dict[str, list[float]]
    algorithms: dict[str, SignalAlgorithmIdentity]


def _empty_samples() -> dict[str, list[float]]:
    """Canonical samples plus decode-only aliases used by old tooling."""

    heart_rate: list[float] = []
    respiration_rate: list[float] = []
    blink_rate: list[float] = []
    open_eye_ratio: list[float] = []
    mouse_velocity: list[float] = []
    mouse_variance: list[float] = []
    neutral_pitch: list[float] = []
    neutral_face_scale: list[float] = []
    quality: list[float] = []
    return {
        "heart_rate_rest": heart_rate,
        "respiration_rate_rest": respiration_rate,
        "blink_rate_work": blink_rate,
        "open_eye_ratio_work": open_eye_ratio,
        "mouse_velocity_work": mouse_velocity,
        "mouse_variance_work": mouse_variance,
        "neutral_head_pitch": neutral_pitch,
        "neutral_face_scale": neutral_face_scale,
        "quality": quality,
        # Compatibility aliases.  They share the same list objects and are
        # never iterated as additional independent evidence.
        "hr": heart_rate,
        "hrv": [],
        "resp": respiration_rate,
        "blink_rate": blink_rate,
        "mouse_velocity": mouse_velocity,
        "mouse_variance": mouse_variance,
    }


def _phase_for_elapsed(elapsed: float, total: float) -> CalibrationPhase:
    fraction = 1.0 if total <= 0 else float(np.clip(elapsed / total, 0.0, 1.0))
    if fraction < 0.10:
        return "camera_quality_check"
    if fraction < 0.55:
        return "physiological_rest"
    return "representative_work"


_PHASE_INSTRUCTIONS: dict[CalibrationPhase, str] = {
    "camera_quality_check": "Center your face and adjust lighting; keep your camera still.",
    "physiological_rest": "Sit naturally and breathe normally; no special breathing pattern is needed.",
    "representative_work": "Use your mouse and keyboard as you normally would while working.",
    "review": "Review what was measured, unavailable, and experimental before saving.",
    "commit": "Saving the approved measured profile and applying it live.",
}


def _fraction_missing(*, scheduled: int, valid: int) -> float:
    if scheduled <= 0:
        return 1.0
    return float(np.clip(1.0 - valid / scheduled, 0.0, 1.0))


def _latest(samples: dict[str, list[float]], name: str) -> float | None:
    values = samples.get(name, [])
    return values[-1] if values else None


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    elapsed: float,
    total: float,
    capture: CalibrationCapture,
    phase: CalibrationPhase,
    lighting_ok: bool,
    motion_ok: bool,
    face_ok: bool,
    status: CalibrationStatus,
    instruction: str | None = None,
) -> None:
    if callback is None:
        return
    scheduled = sum(capture.phase_scheduled_count.values())
    valid = sum(capture.phase_valid_count.values())
    snapshot = CalibrationProgress(
        elapsed_seconds=max(0.0, elapsed),
        total_seconds=max(0.0, total),
        current_hr=_latest(capture.samples, "heart_rate_rest"),
        # HRV is intentionally unavailable pending reference validation.
        current_hrv=None,
        current_sqi=_latest(capture.samples, "quality"),
        lighting_ok=lighting_ok,
        motion_ok=motion_ok,
        face_ok=face_ok,
        pct_complete=(0.0 if total <= 0 else float(np.clip(elapsed / total * 100.0, 0.0, 100.0))),
        status=status,
        phase=phase,
        phase_instruction=instruction or _PHASE_INSTRUCTIONS[phase],
        valid_duration_seconds=sum(capture.phase_valid_duration_seconds.values()),
        missing_fraction=_fraction_missing(scheduled=scheduled, valid=valid),
    )
    try:
        callback(snapshot)
    except Exception:
        logger.debug("calibration progress callback raised", exc_info=True)


def _empty_capture() -> CalibrationCapture:
    phases = (
        "camera_quality_check",
        "physiological_rest",
        "representative_work",
    )
    return CalibrationCapture(
        samples=_empty_samples(),
        camera=None,
        phase_valid_duration_seconds=dict.fromkeys(phases, 0.0),
        phase_scheduled_count=dict.fromkeys(phases, 0),
        phase_valid_count=dict.fromkeys(phases, 0),
        phase_quality={phase: [] for phase in phases},
        algorithms={},
    )


async def _collect_simulated_calibration(
    duration_seconds: int,
    *,
    is_aborted: Callable[[], bool] | None,
    on_progress: ProgressCallback | None,
    clock: Clock,
) -> CalibrationCapture:
    rng = random.Random(42)
    capture = _empty_capture()
    total_ticks = max(1, int(duration_seconds * PROGRESS_HZ))
    interval = 1.0 / PROGRESS_HZ
    previous_elapsed = 0.0
    _emit_progress(
        on_progress,
        elapsed=0.0,
        total=float(duration_seconds),
        capture=capture,
        phase="camera_quality_check",
        lighting_ok=True,
        motion_ok=True,
        face_ok=True,
        status="initializing",
    )
    for tick in range(total_ticks):
        if is_aborted is not None and is_aborted():
            break
        elapsed = min(float(duration_seconds), tick * interval)
        phase = _phase_for_elapsed(elapsed, float(duration_seconds))
        capture.phase_scheduled_count[phase] += 1
        capture.phase_valid_count[phase] += 1
        capture.phase_valid_duration_seconds[phase] += max(0.0, elapsed - previous_elapsed)
        capture.phase_quality[phase].append(0.95)
        capture.samples["quality"].append(0.95)
        if phase in {"camera_quality_check", "physiological_rest"}:
            capture.samples["neutral_head_pitch"].append(rng.gauss(2.0, 0.7))
            capture.samples["neutral_face_scale"].append(rng.gauss(180.0, 2.0))
        if phase == "physiological_rest":
            capture.samples["heart_rate_rest"].append(rng.gauss(70.0, 3.0))
            capture.samples["respiration_rate_rest"].append(rng.gauss(15.0, 1.0))
        if phase == "representative_work":
            capture.samples["blink_rate_work"].append(rng.gauss(17.0, 2.0))
            capture.samples["open_eye_ratio_work"].append(rng.gauss(0.32, 0.01))
            capture.samples["mouse_velocity_work"].append(rng.gauss(500.0, 100.0))
            capture.samples["mouse_variance_work"].append(rng.gauss(10_000.0, 2_000.0))
        previous_elapsed = elapsed
        _emit_progress(
            on_progress,
            elapsed=elapsed,
            total=float(duration_seconds),
            capture=capture,
            phase=phase,
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="running",
        )
        await asyncio.sleep(interval)
    return capture


async def run_simulate_calibration(
    duration_seconds: int,
    *,
    is_aborted: Callable[[], bool] | None = None,
    on_progress: ProgressCallback | None = None,
    clock: Clock | None = None,
) -> dict[str, list[float]]:
    """Compatibility wrapper returning demo samples without persisting them."""

    capture = await _collect_simulated_calibration(
        duration_seconds,
        is_aborted=is_aborted,
        on_progress=on_progress,
        clock=clock or SYSTEM_CLOCK,
    )
    return capture.samples


async def _collect_live_calibration(
    duration_seconds: int,
    *,
    config: CortexConfig,
    is_aborted: Callable[[], bool] | None,
    on_progress: ProgressCallback | None,
    on_unavailable: Callable[[], None] | None,
    clock: Clock,
) -> CalibrationCapture:
    # Heavy/native dependencies remain lazy so schema/store/CLI help work in
    # headless test environments.
    from cortex.services.capture_service.feature_factory import (
        build_production_camera_feature_components,
        production_calibration_algorithm_identities,
    )
    from cortex.services.capture_service.observation_buffer import (
        NumericObservation,
        ObservationBuffer,
        prepare_observation_window,
    )
    from cortex.services.capture_service.pipeline import CapturePipeline
    from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
    from cortex.services.telemetry_engine.input_hooks import InputHooks

    capture = _empty_capture()
    components = build_production_camera_feature_components(config)
    pipeline = CapturePipeline(config.capture, clock=clock)
    input_hooks = InputHooks(config.telemetry, clock=clock)
    aggregator = FeatureAggregator(input_hooks, config=config.telemetry, clock=clock)
    rgb_window_seconds = max(
        float(config.signal.rppg.window_seconds),
        float(config.signal.rppg.respiration_window_seconds),
    )
    observations: ObservationBuffer[NumericObservation] = ObservationBuffer(
        max_age_seconds=(
            rgb_window_seconds + config.signal.rppg.max_interpolation_gap_ms / 1000.0 + 1.0
        ),
        max_items=max(
            config.capture.observation_buffer_max_items,
            int(rgb_window_seconds * config.signal.rppg.fps_clamp_max) + 2,
        ),
    )
    capture.algorithms = production_calibration_algorithm_identities(
        config,
        components=components,
    )

    try:
        await pipeline.start()
    except Exception as exc:
        if on_unavailable is not None:
            on_unavailable()
        raise CalibrationCaptureUnavailable(
            "live camera capture could not start; no calibration was saved"
        ) from exc

    hooks_started = input_hooks.start()
    start_ns = clock.monotonic_ns()
    last_progress_elapsed = -1.0
    last_physio_ns = 0
    previous_valid_ns: int | None = None
    previous_phase: CalibrationPhase | None = None
    active_phase: CalibrationPhase | None = None
    latest_lighting_ok = False
    latest_motion_ok = False
    latest_face_ok = False
    try:
        _emit_progress(
            on_progress,
            elapsed=0.0,
            total=float(duration_seconds),
            capture=capture,
            phase="camera_quality_check",
            lighting_ok=False,
            motion_ok=False,
            face_ok=False,
            status="initializing",
        )
        while True:
            if is_aborted is not None and is_aborted():
                break
            elapsed = (clock.monotonic_ns() - start_ns) / 1_000_000_000.0
            if elapsed >= duration_seconds:
                break
            output = await pipeline.get_output(timeout=0.5)
            if output is None:
                if elapsed - last_progress_elapsed >= 1.0 / PROGRESS_HZ:
                    phase = _phase_for_elapsed(elapsed, float(duration_seconds))
                    _emit_progress(
                        on_progress,
                        elapsed=elapsed,
                        total=float(duration_seconds),
                        capture=capture,
                        phase=phase,
                        lighting_ok=False,
                        motion_ok=False,
                        face_ok=False,
                        status="running",
                    )
                    last_progress_elapsed = elapsed
                continue

            observation = output.observation
            mono_ns = observation.observed_at_mono_ns
            mono_seconds = mono_ns / 1_000_000_000.0
            elapsed = (mono_ns - start_ns) / 1_000_000_000.0
            if elapsed < 0:
                continue
            phase = _phase_for_elapsed(elapsed, float(duration_seconds))
            if active_phase != phase:
                observations.clear()
                components.physiology.reset()
                if phase == "representative_work":
                    components.blink.reset()
                    input_hooks.reset()
                previous_valid_ns = None
                previous_phase = None
                active_phase = phase

            capture.phase_scheduled_count[phase] += 1
            capture.camera = output.camera_identity
            latest_face_ok = bool(
                observation.validity == ObservationValidity.VALID.value
                and output.landmarks_px is not None
            )
            latest_lighting_ok = output.quality.brightness_score >= 0.2
            latest_motion_ok = output.quality.motion_score >= 0.3
            valid = latest_face_ok and output.frame is not None
            rgb_value: NDArray[np.float64] | None = None
            if valid:
                assert output.landmarks_px is not None
                assert output.frame is not None
                roi = components.roi_extractor.extract(
                    output.frame,
                    output.landmarks_px,
                    mono_seconds,
                )
                combined = roi.combined_rgb()
                if combined is not None and bool(np.isfinite(combined).all()):
                    rgb_value = np.asarray(combined, dtype=np.float64)
                else:
                    valid = False
            # Motion evidence in face widths/second (resolution and FPS
            # independent) computed by the face tracker for this frame.
            motion_fw_s = (
                getattr(observation.value, "motion_face_widths_per_second", None)
                if valid
                else None
            )

            numeric = NumericObservation(
                observed_at_unix_ms=observation.observed_at_unix_ms,
                observed_at_mono_ns=mono_ns,
                boot_id=observation.boot_id,
                sequence=observation.sequence,
                value=rgb_value if valid else None,
                validity=(
                    ObservationValidity.VALID.value if valid else ObservationValidity.REJECTED.value
                ),
                missing_reason=(
                    None
                    if valid
                    else observation.missing_reason or MissingReason.ARTIFACT
                ),
                quality=observation.quality if valid else 0.0,
                motion_face_widths_per_second=motion_fw_s,
            )
            observations.append(numeric)

            if not valid or output.landmarks_px is None:
                components.blink.observe_missing(mono_seconds)
                components.head_pose.observe_missing(mono_seconds)
                previous_valid_ns = None
            else:
                capture.phase_valid_count[phase] += 1
                capture.phase_quality[phase].append(observation.quality)
                capture.samples["quality"].append(observation.quality)
                if (
                    previous_valid_ns is not None
                    and previous_phase == phase
                    and mono_ns - previous_valid_ns
                    <= int(config.signal.blink.max_valid_gap_ms * 1_000_000)
                ):
                    capture.phase_valid_duration_seconds[phase] += (
                        mono_ns - previous_valid_ns
                    ) / 1_000_000_000.0
                previous_valid_ns = mono_ns
                previous_phase = phase

                blink = components.blink.update(output.landmarks_px, mono_seconds)
                pose = components.head_pose.update(output.landmarks_px, mono_seconds)
                if phase in {"camera_quality_check", "physiological_rest"}:
                    if not pose.is_jittery and observation.quality >= 0.5:
                        capture.samples["neutral_head_pitch"].append(pose.pitch)
                        try:
                            scale = components.head_neck_proxy.face_scale(output.landmarks_px)
                        except ValueError:
                            scale = None
                        if scale is not None:
                            capture.samples["neutral_face_scale"].append(scale)
                if phase == "representative_work":
                    if blink.blink_rate is not None:
                        capture.samples["blink_rate_work"].append(blink.blink_rate)
                    if not blink.is_closed:
                        capture.samples["open_eye_ratio_work"].append(blink.ear_mean)

            stride_ns = int(config.signal.rppg.stride_seconds * 1_000_000_000)
            if phase == "physiological_rest" and (
                last_physio_ns == 0 or mono_ns - last_physio_ns >= stride_ns
            ):
                cfg = config.signal.rppg
                prepared = prepare_observation_window(
                    observations.snapshot(),
                    window_seconds=float(cfg.window_seconds),
                    nominal_fps=float(config.capture.fps),
                    min_valid_fraction=cfg.min_valid_coverage,
                    max_interpolation_gap_ms=cfg.max_interpolation_gap_ms,
                    max_motion_fraction=cfg.max_motion_rejected_fraction,
                    fps_clamp_min=cfg.fps_clamp_min,
                    fps_clamp_max=cfg.fps_clamp_max,
                )
                last_physio_ns = mono_ns
                if (
                    prepared.ready
                    and prepared.values is not None
                    and prepared.sample_times_mono_ns is not None
                ):
                    pulse = components.physiology.pulse.process_window(
                        prepared.values,
                        prepared.sample_times_mono_ns,
                        sample_rate_hz=prepared.sample_rate_hz,
                        boot_id=observation.boot_id,
                        observation_quality=prepared.quality,
                        motion_face_widths_per_second=(
                            prepared.mean_motion_face_widths_per_second
                        ),
                        face_presence_ratio=prepared.valid_fraction,
                    )
                    capture.algorithms["physiology"] = pulse.summary.algorithm
                    if pulse.summary.hr.value is not None:
                        capture.samples["heart_rate_rest"].append(pulse.summary.hr.value)

                    resp_window = prepare_observation_window(
                        observations.snapshot(),
                        window_seconds=float(cfg.respiration_window_seconds),
                        nominal_fps=float(config.capture.fps),
                        min_valid_fraction=cfg.min_valid_coverage,
                        max_interpolation_gap_ms=cfg.max_interpolation_gap_ms,
                        max_motion_fraction=cfg.max_motion_rejected_fraction,
                        fps_clamp_min=cfg.fps_clamp_min,
                        fps_clamp_max=cfg.fps_clamp_max,
                    )
                    if (
                        resp_window.ready
                        and resp_window.values is not None
                        and resp_window.sample_times_mono_ns is not None
                    ):
                        respiration = components.physiology.respiration.process_window(
                            resp_window.values,
                            resp_window.sample_times_mono_ns,
                            sample_rate_hz=resp_window.sample_rate_hz,
                            boot_id=observation.boot_id,
                            head_vertical_face_units=(resp_window.head_vertical_face_units),
                        )
                        if respiration.fused.value is not None:
                            capture.samples["respiration_rate_rest"].append(respiration.fused.value)

            if (
                phase == "representative_work"
                and hooks_started
                and elapsed - last_progress_elapsed >= 1.0 / PROGRESS_HZ
            ):
                telemetry = aggregator.build_features(
                    window_seconds=min(
                        config.telemetry.window_seconds,
                        max(1.0, capture.phase_valid_duration_seconds[phase]),
                    ),
                    current_time=monotonic_seconds(clock),
                )
                # A zero value can be a valid observation.  Input permission,
                # not numeric magnitude, decides availability.
                capture.samples["mouse_velocity_work"].append(telemetry.mouse_velocity_mean)
                capture.samples["mouse_variance_work"].append(telemetry.mouse_velocity_variance)

            if elapsed - last_progress_elapsed >= 1.0 / PROGRESS_HZ:
                _emit_progress(
                    on_progress,
                    elapsed=elapsed,
                    total=float(duration_seconds),
                    capture=capture,
                    phase=phase,
                    lighting_ok=latest_lighting_ok,
                    motion_ok=latest_motion_ok,
                    face_ok=latest_face_ok,
                    status="running",
                )
                last_progress_elapsed = elapsed
    finally:
        try:
            input_hooks.stop()
        except Exception:
            logger.debug("input hooks failed to stop", exc_info=True)
        try:
            await pipeline.stop()
        except Exception:
            logger.debug("calibration capture pipeline failed to stop", exc_info=True)

    if capture.camera is None:
        if on_unavailable is not None:
            on_unavailable()
        raise CalibrationCaptureUnavailable(
            "no camera observations were captured; no calibration was saved"
        )
    if sum(capture.phase_valid_count.values()) == 0:
        raise CalibrationCaptureUnavailable(
            "no quality-gated face observations were captured; no calibration was saved"
        )
    return capture


async def run_live_calibration(
    duration_seconds: int,
    *,
    config: CortexConfig | None = None,
    is_aborted: Callable[[], bool] | None = None,
    on_progress: ProgressCallback | None = None,
    on_fallback: Callable[[], None] | None = None,
    clock: Clock | None = None,
) -> dict[str, list[float]]:
    """Compatibility wrapper; live failure raises instead of fabricating data."""

    capture = await _collect_live_calibration(
        duration_seconds,
        config=config or get_config(),
        is_aborted=is_aborted,
        on_progress=on_progress,
        on_unavailable=on_fallback,
        clock=clock or SYSTEM_CLOCK,
    )
    return capture.samples


def _distribution(values: list[float]) -> CalibrationDistribution | None:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=np.float64)
    if finite.size == 0:
        return None
    return CalibrationDistribution(
        mean=float(np.mean(finite)),
        std=float(np.std(finite, ddof=1)) if finite.size >= 2 else 0.0,
        p10=float(np.percentile(finite, 10)),
        median=float(np.median(finite)),
        p90=float(np.percentile(finite, 90)),
    )


def _circular_distribution_deg(values: list[float]) -> CalibrationDistribution | None:
    """Distribution of angles (degrees) centred on their circular mean.

    ``mean`` is the circular mean in ``[-180, 180]``; spread and percentiles
    are computed on the samples unwrapped around that mean so a neutral pose
    straddling +/-180 deg is summarised as a tight cluster rather than as a
    bimodal distribution with a meaningless linear mean.
    """

    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=np.float64)
    if finite.size == 0:
        return None
    mean = circular_mean_deg(finite)
    unwrapped = mean + np.asarray(
        [wrapped_angle_delta(float(value), mean) for value in finite], dtype=np.float64
    )
    return CalibrationDistribution(
        mean=mean,
        std=float(np.std(unwrapped, ddof=1)) if unwrapped.size >= 2 else 0.0,
        p10=float(np.percentile(unwrapped, 10)),
        median=float(np.median(unwrapped)),
        p90=float(np.percentile(unwrapped, 90)),
    )


def _quality_percentiles(values: list[float]) -> tuple[float, float, float]:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=np.float64)
    if finite.size == 0:
        return 0.0, 0.0, 0.0
    return (
        float(np.percentile(finite, 10)),
        float(np.median(finite)),
        float(np.percentile(finite, 90)),
    )


def _metric_summary(
    capture: CalibrationCapture,
    *,
    metric: CalibrationMetricName,
    sample_key: str,
    unit: str,
    task: CalibrationReferenceTask,
    maturity: CalibrationMetricMaturity,
    algorithm_key: str,
    effective_window_seconds: float,
    circular_degrees: bool = False,
) -> CalibrationMetricSummary:
    samples = capture.samples[sample_key]
    distribution = (
        _circular_distribution_deg(samples) if circular_degrees else _distribution(samples)
    )
    evidence_phases = (
        ("camera_quality_check", "physiological_rest")
        if task == CalibrationReferenceTask.NEUTRAL_HEAD_POSE
        else (task.value,)
    )
    valid_duration = sum(
        capture.phase_valid_duration_seconds.get(phase, 0.0)
        for phase in evidence_phases
    )
    scheduled = sum(
        capture.phase_scheduled_count.get(phase, 0) for phase in evidence_phases
    )
    valid = sum(capture.phase_valid_count.get(phase, 0) for phase in evidence_phases)
    quality_values = [
        value
        for phase in evidence_phases
        for value in capture.phase_quality.get(phase, [])
    ]
    q10, q50, q90 = _quality_percentiles(quality_values)
    algorithm = capture.algorithms.get(algorithm_key)
    if algorithm is None:
        algorithm = SignalAlgorithmIdentity(
            name=f"{algorithm_key}-unavailable",
            version="2.0.0",
            implementation_sha256="0" * 64,
            configuration_sha256="0" * 64,
            selection_mode="fixed",
        )
    if distribution is None:
        return CalibrationMetricSummary(
            metric=metric,
            unit=unit,
            reference_task=task,
            maturity=CalibrationMetricMaturity.UNAVAILABLE,
            sample_count=0,
            effective_sample_count=0.0,
            valid_duration_seconds=valid_duration,
            missing_fraction=_fraction_missing(scheduled=scheduled, valid=valid),
            quality_p10=q10,
            quality_median=q50,
            quality_p90=q90,
            algorithm=algorithm,
            unavailable_reason="no independent quality-gated observations",
        )
    effective_count = min(
        float(len(samples)),
        valid_duration / max(0.001, effective_window_seconds),
    )
    return CalibrationMetricSummary(
        metric=metric,
        unit=unit,
        reference_task=task,
        maturity=maturity,
        value=distribution.mean,
        distribution=distribution,
        sample_count=len(samples),
        effective_sample_count=effective_count,
        valid_duration_seconds=valid_duration,
        missing_fraction=_fraction_missing(scheduled=scheduled, valid=valid),
        quality_p10=q10,
        quality_median=q50,
        quality_p90=q90,
        algorithm=algorithm,
    )


def compute_baselines(
    samples: dict[str, list[float]],
    *,
    clock: Clock | None = None,
) -> UserBaselines:
    """Decode-only legacy view derived only from present observations."""

    def _values(primary: str, alias: str) -> list[float]:
        return samples.get(primary) or samples.get(alias, [])

    values: dict[str, object] = {}
    hr = _values("heart_rate_rest", "hr")
    blink = _values("blink_rate_work", "blink_rate")
    mouse_velocity = _values("mouse_velocity_work", "mouse_velocity")
    mouse_variance = _values("mouse_variance_work", "mouse_variance")
    respiration = _values("respiration_rate_rest", "resp")
    if hr:
        values["hr_baseline"] = statistics.mean(hr)
        values["hr_std"] = max(1.0, min(20.0, statistics.stdev(hr) if len(hr) > 1 else 1.0))
    if blink:
        values["blink_rate_baseline"] = statistics.mean(blink)
    if mouse_velocity:
        values["mouse_velocity_baseline"] = statistics.mean(mouse_velocity)
    if mouse_variance:
        values["mouse_variance_baseline"] = statistics.mean(mouse_variance)
    if respiration:
        values["resp_baseline"] = statistics.mean(respiration)
    if any((hr, blink, mouse_velocity, mouse_variance, respiration)):
        values["calibrated_at"] = utc_datetime(clock or SYSTEM_CLOCK)
    return UserBaselines.model_validate(values)


def baselines_dir(config: CortexConfig | None = None) -> Path:
    """Legacy compatibility directory; active provenance lives in calibration/."""

    cfg = config or get_config()
    return Path(cfg.storage.path).expanduser() / "baselines"


def default_baseline_path(config: CortexConfig | None = None) -> Path:
    return baselines_dir(config) / "default.json"


def calibration_review_payload(profile: CalibrationProfile) -> dict[str, object]:
    """Small transport-neutral review model with no sensitive raw samples."""

    return {
        "profile_id": str(profile.profile_id),
        "provenance": str(profile.provenance),
        "camera_name": profile.camera.device_name if profile.camera else None,
        "metrics": [
            {
                "name": str(metric.metric),
                "value": metric.value,
                "unit": metric.unit,
                "maturity": str(metric.maturity),
                "reference_task": str(metric.reference_task),
                "valid_duration_seconds": metric.valid_duration_seconds,
                "missing_fraction": metric.missing_fraction,
                "quality_median": metric.quality_median,
            }
            for metric in profile.metrics
        ],
        "notes": list(profile.notes),
    }


class CalibrationRunner:
    """Collect, review, and explicitly commit one calibration profile."""

    def __init__(
        self,
        duration_seconds: int = DEFAULT_DURATION_SECONDS,
        simulate: bool = False,
        config: CortexConfig | None = None,
        *,
        output_path: Path | str | None = None,
        clock: Clock | None = None,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        self.duration_seconds = int(duration_seconds)
        self.simulate = bool(simulate)
        self.used_simulation = bool(simulate)
        self._clock = clock or SYSTEM_CLOCK
        self._config = config or get_config()
        self._store = CalibrationProfileStore(
            self._config.storage.path,
            clock=self._clock,
        )
        self._output_override = Path(output_path).expanduser() if output_path else None
        self._profile_id = uuid4()
        self._capture: CalibrationCapture | None = None
        self._aborted = False
        self._started = False
        self._finished = False
        self._committed_profile: CalibrationProfile | None = None
        self._last_progress: CalibrationProgress | None = None
        self._progress_callback: ProgressCallback | None = None

    @property
    def last_progress(self) -> CalibrationProgress | None:
        return self._last_progress

    @property
    def is_running(self) -> bool:
        return self._started and self._capture is None and not self._aborted

    def abort(self) -> None:
        self._aborted = True

    def _on_progress(self, user_callback: ProgressCallback | None) -> ProgressCallback:
        def _inner(progress: CalibrationProgress) -> None:
            self._last_progress = progress
            if user_callback is not None:
                user_callback(progress)

        return _inner

    async def start(self, on_progress: ProgressCallback | None = None) -> None:
        if self._started:
            raise RuntimeError("CalibrationRunner.start() already called")
        self._started = True
        self._progress_callback = self._on_progress(on_progress)
        try:
            if self.simulate:
                self._capture = await _collect_simulated_calibration(
                    self.duration_seconds,
                    is_aborted=lambda: self._aborted,
                    on_progress=self._progress_callback,
                    clock=self._clock,
                )
            else:
                self._capture = await _collect_live_calibration(
                    self.duration_seconds,
                    config=self._config,
                    is_aborted=lambda: self._aborted,
                    on_progress=self._progress_callback,
                    on_unavailable=None,
                    clock=self._clock,
                )
        except Exception:
            _emit_progress(
                self._progress_callback,
                elapsed=float(self.duration_seconds),
                total=float(self.duration_seconds),
                capture=self._capture or _empty_capture(),
                phase="review",
                lighting_ok=False,
                motion_ok=False,
                face_ok=False,
                status="failed",
            )
            raise

        if self._aborted:
            _emit_progress(
                self._progress_callback,
                elapsed=self._last_progress.elapsed_seconds if self._last_progress else 0.0,
                total=float(self.duration_seconds),
                capture=self._capture,
                phase="review",
                lighting_ok=False,
                motion_ok=False,
                face_ok=False,
                status="aborted",
            )
            return
        _emit_progress(
            self._progress_callback,
            elapsed=float(self.duration_seconds),
            total=float(self.duration_seconds),
            capture=self._capture,
            phase="review",
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="review_required",
        )

    def preview_profile(self) -> CalibrationProfile:
        if not self._started or self._capture is None:
            raise RuntimeError("calibration has no completed capture to review")
        if self._aborted:
            raise RuntimeError("aborted calibration cannot produce a profile")
        provenance = (
            CalibrationProvenance.DEMO if self.used_simulation else CalibrationProvenance.MEASURED
        )
        capture = self._capture
        rppg_window = float(self._config.signal.rppg.window_seconds)
        telemetry_window = float(self._config.telemetry.window_seconds)
        metrics = (
            _metric_summary(
                capture,
                metric=CalibrationMetricName.HEART_RATE_BPM,
                sample_key="heart_rate_rest",
                unit="bpm",
                task=CalibrationReferenceTask.PHYSIOLOGICAL_REST,
                maturity=CalibrationMetricMaturity.EXPERIMENTAL,
                algorithm_key="physiology",
                effective_window_seconds=rppg_window,
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.RESPIRATION_RATE_BPM,
                sample_key="respiration_rate_rest",
                unit="breaths/min",
                task=CalibrationReferenceTask.PHYSIOLOGICAL_REST,
                maturity=CalibrationMetricMaturity.EXPERIMENTAL,
                algorithm_key="physiology",
                effective_window_seconds=float(self._config.signal.rppg.respiration_window_seconds),
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.BLINK_RATE_PER_MIN,
                sample_key="blink_rate_work",
                unit="blinks/min",
                task=CalibrationReferenceTask.REPRESENTATIVE_WORK,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="blink",
                effective_window_seconds=(self._config.signal.blink.min_valid_exposure_seconds),
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.OPEN_EYE_RATIO,
                sample_key="open_eye_ratio_work",
                unit="ratio",
                task=CalibrationReferenceTask.REPRESENTATIVE_WORK,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="blink",
                effective_window_seconds=1.0,
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S,
                sample_key="mouse_velocity_work",
                unit="px/s",
                task=CalibrationReferenceTask.REPRESENTATIVE_WORK,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="telemetry",
                effective_window_seconds=telemetry_window,
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.MOUSE_VELOCITY_VARIANCE,
                sample_key="mouse_variance_work",
                unit="(px/s)^2",
                task=CalibrationReferenceTask.REPRESENTATIVE_WORK,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="telemetry",
                effective_window_seconds=telemetry_window,
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG,
                sample_key="neutral_head_pitch",
                unit="deg",
                task=CalibrationReferenceTask.NEUTRAL_HEAD_POSE,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="head_pose",
                effective_window_seconds=1.0,
                circular_degrees=True,
            ),
            _metric_summary(
                capture,
                metric=CalibrationMetricName.NEUTRAL_FACE_SCALE_PX,
                sample_key="neutral_face_scale",
                unit="px",
                task=CalibrationReferenceTask.NEUTRAL_HEAD_POSE,
                maturity=CalibrationMetricMaturity.OBSERVED,
                algorithm_key="head_pose",
                effective_window_seconds=1.0,
            ),
        )

        values_by_name = {
            str(metric.metric): metric.value
            for metric in metrics
            if metric.maturity != CalibrationMetricMaturity.UNAVAILABLE.value
        }
        camera = None
        if capture.camera is not None:
            camera = CalibrationCameraIdentity(
                identity_key=capture.camera.identity_key,
                device_name=capture.camera.device_name,
                source=capture.camera.source,
                width=capture.camera.width,
                height=capture.camera.height,
            )
        return CalibrationProfile(
            profile_id=self._profile_id,
            provenance=provenance,
            created_at_unix_ms=self._clock.unix_ms(),
            approved_at_unix_ms=None,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            protocol_version=CALIBRATION_PROTOCOL_VERSION,
            camera=camera,
            metrics=metrics,
            baselines=CalibrationBaselineValues(
                heart_rate_bpm=values_by_name.get(CalibrationMetricName.HEART_RATE_BPM.value),
                respiration_rate_bpm=values_by_name.get(
                    CalibrationMetricName.RESPIRATION_RATE_BPM.value
                ),
                blink_rate_per_min=values_by_name.get(
                    CalibrationMetricName.BLINK_RATE_PER_MIN.value
                ),
                open_eye_ratio=values_by_name.get(CalibrationMetricName.OPEN_EYE_RATIO.value),
                mouse_velocity_px_per_s=values_by_name.get(
                    CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S.value
                ),
                mouse_velocity_variance=values_by_name.get(
                    CalibrationMetricName.MOUSE_VELOCITY_VARIANCE.value
                ),
                neutral_head_pitch_deg=values_by_name.get(
                    CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value
                ),
                neutral_face_scale_px=values_by_name.get(
                    CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value
                ),
            ),
            notes=(
                "Webcam heart and respiration estimates remain experimental and are not applied to production scoring.",
                "No raw frames, landmarks, keys, or workspace content are stored.",
            ),
        )

    async def finish(self, *, approved_by_user: bool) -> CalibrationProfile:
        """Stage an explicitly approved, live measured profile for activation.

        Persisting the immutable candidate and changing the active pointer are
        deliberately separate operations.  The running daemon first validates
        the candidate and constructs every replacement dependency, then commits
        the pointer and swaps the prepared graph without an await boundary.  A
        rejected candidate therefore cannot leave persistence and the live
        process disagreeing about which profile is active.
        """

        if not approved_by_user:
            raise ValueError("calibration commit requires explicit user approval")
        if self._finished:
            raise RuntimeError("CalibrationRunner.finish() already called")
        profile = self.preview_profile()
        if profile.provenance != CalibrationProvenance.MEASURED.value:
            raise RuntimeError("simulation is demo-only and can never become active calibration")
        approved = profile.model_copy(update={"approved_at_unix_ms": self._clock.unix_ms()})
        await asyncio.to_thread(self._store.save_inactive, approved)
        if self._output_override is not None:
            await asyncio.to_thread(
                atomic_write_json,
                self._output_override,
                approved.model_dump(mode="json"),
            )
        self._finished = True
        self._committed_profile = approved
        assert self._capture is not None
        _emit_progress(
            self._progress_callback,
            elapsed=float(self.duration_seconds),
            total=float(self.duration_seconds),
            capture=self._capture,
            phase="commit",
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="applying",
        )
        return approved

    def activate_for_next_start(self, profile_id: str | None = None) -> None:
        """Activate a staged profile when no live daemon is available.

        This is intentionally an offline/CLI escape hatch.  Desktop transports
        must ask the daemon to activate the candidate so all dependent services
        switch in the same transaction.  The CLI reports that this path takes
        effect on the next daemon start rather than pretending a running daemon
        was reconfigured.
        """

        profile = self._committed_profile
        if profile is None or self._capture is None:
            raise RuntimeError("no committed calibration is awaiting activation")
        if profile_id is not None and str(profile.profile_id) != str(profile_id):
            raise ValueError("calibration profile does not match staged commit")
        self._store.activate(profile)

    def mark_failed(self, reason: str) -> None:
        """Publish a terminal failure without claiming an unverified outcome."""

        if self._capture is None:
            raise RuntimeError("no calibration capture is available")
        _emit_progress(
            self._progress_callback,
            elapsed=float(self.duration_seconds),
            total=float(self.duration_seconds),
            capture=self._capture,
            phase="commit",
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="failed",
            instruction=(
                "Calibration application was not confirmed. "
                f"{str(reason).strip()}"
            ).strip(),
        )

    def is_committed_profile_active(self, profile_id: str | None = None) -> bool:
        """Reconcile a lost acknowledgement against the authoritative pointer."""

        profile = self._committed_profile
        if profile is None:
            return False
        if profile_id is not None and str(profile.profile_id) != str(profile_id):
            return False
        try:
            active = self._store.load_active()
        except (OSError, ValueError):
            return False
        return bool(
            active is not None
            and active.profile_id == profile.profile_id
            and calibration_profile_sha256(active)
            == calibration_profile_sha256(profile)
        )

    def mark_applied(self, profile_id: str | None = None) -> None:
        """Emit completion only after the running daemon confirms the swap."""

        profile = self._committed_profile
        if profile is None or self._capture is None:
            raise RuntimeError("no committed calibration is awaiting application")
        if profile_id is not None and str(profile.profile_id) != str(profile_id):
            raise ValueError("applied calibration profile does not match commit")
        _emit_progress(
            self._progress_callback,
            elapsed=float(self.duration_seconds),
            total=float(self.duration_seconds),
            capture=self._capture,
            phase="commit",
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="completed",
        )

    async def save_demo(self) -> CalibrationProfile:
        """Persist a demo artifact in its isolated namespace, never active."""

        profile = self.preview_profile()
        if profile.provenance != CalibrationProvenance.DEMO.value:
            raise RuntimeError("save_demo is available only for simulation")
        await asyncio.to_thread(self._store.save_demo, profile)
        return profile

    def _write_baselines(self, baselines: UserBaselines) -> Path:
        """Decode-only helper retained for old external tooling."""

        destination = self._output_override or (
            baselines_dir(self._config)
            / f"baseline_{utc_datetime(self._clock).strftime('%Y%m%d_%H%M%S')}.json"
        )
        atomic_write_json(destination, baselines.model_dump(mode="json"))
        return destination
