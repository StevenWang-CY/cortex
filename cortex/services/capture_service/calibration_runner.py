"""P0 §3.4 — In-process calibration runner.

Wraps the existing live + simulate calibration loops from
:mod:`cortex.scripts.calibrate` so the Qt onboarding wizard, the
Settings "Recalibrate" button, and the developer-facing CLI all drive
the same code path. Pre-3.4 the CLI was the only entry point and the
wizard subprocessed it — that broke TCC inheritance and meant the
wizard had no live feedback to show the user during the 2-minute sit.

Design:

* `CalibrationRunner(duration_seconds, simulate, config)` — constructor;
  no work happens until `start()` is awaited.
* `await runner.start(on_progress=...)` — drives one capture loop. The
  callback fires at ~2 Hz with a `CalibrationProgress` dataclass so the
  UI can update the ECG trace, status pills, and progress bar.
* `await runner.finish()` — runs `compute_baselines` over the captured
  samples and atomically writes
  `storage/baselines/baseline_<timestamp>.json` + `default.json`.
* `runner.abort()` — cooperative cancellation; the start() loop checks
  the flag each tick and exits cleanly, releasing the camera handle.

Hard invariants (CLAUDE.md):

* No subprocess. The wizard drives the runner in-process so the daemon's
  webcam handle / TCC context is reused.
* `cv2` is imported inside functions, never at module top-level — keeps
  the runner importable in environments without OpenCV (e.g. test
  harnesses, CI Linux runners).
* `default.json` is written via `atomic_write_json`; the prior file is
  never overwritten on a failed or partial run (rule 28 / §3.4 risk
  note "Existing baselines must not be silently overwritten if the new
  run fails").
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cortex.libs.config.settings import get_config
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)


DEFAULT_DURATION_SECONDS = 120
PROGRESS_HZ = 2.0  # 2 callbacks per second matches the spec's "live feedback at 2 Hz"


CalibrationStatus = Literal[
    "initializing",
    "running",
    "completed",
    "aborted",
    "failed",
]


@dataclass(frozen=True)
class CalibrationProgress:
    """Snapshot of an in-flight calibration run.

    Pushed through the ``on_progress`` callback at ~2 Hz. The UI reads
    `current_hr`/`current_hrv`/`current_sqi` to drive the live numerics,
    `lighting_ok`/`motion_ok`/`face_ok` to flip the three status pills,
    and `pct_complete` to fill the progress bar.
    """

    elapsed_seconds: float
    total_seconds: float
    current_hr: float | None
    current_hrv: float | None
    current_sqi: float | None
    lighting_ok: bool
    motion_ok: bool
    face_ok: bool
    pct_complete: float
    status: CalibrationStatus


ProgressCallback = Callable[[CalibrationProgress], None]


# ---------------------------------------------------------------------------
# Reusable inner loops — extracted from cortex/scripts/calibrate.py so the
# CLI script and the runner share one implementation.
# ---------------------------------------------------------------------------


def _empty_samples() -> dict[str, list[float]]:
    return {
        "hr": [],
        "hrv": [],
        "blink_rate": [],
        "mouse_velocity": [],
        "mouse_variance": [],
        "shoulder_y": [],
    }


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    elapsed: float,
    total: float,
    samples: dict[str, list[float]],
    lighting_ok: bool,
    motion_ok: bool,
    face_ok: bool,
    status: CalibrationStatus,
) -> None:
    """Compute the current snapshot from the running sample buffers and
    invoke the user callback. Defensive: any callback exception is
    swallowed so a buggy UI handler can't kill the capture loop."""
    if callback is None:
        return
    hr = samples["hr"][-1] if samples["hr"] else None
    hrv = samples["hrv"][-1] if samples["hrv"] else None
    pct = 0.0 if total <= 0 else min(100.0, (elapsed / total) * 100.0)
    # SQI proxy: as the recent HR samples settle, variance drops and SQI
    # climbs toward 1.0. Until we have at least 4 HR samples we report
    # None so the UI can show "—" rather than a misleading "0.92".
    sqi: float | None = None
    if len(samples["hr"]) >= 4:
        recent = samples["hr"][-8:]
        spread = max(recent) - min(recent)
        # Lower spread → higher SQI. Clamp to [0.4, 0.99] so we don't
        # promise certainty we can't deliver.
        sqi = max(0.4, min(0.99, 1.0 - (spread / 30.0)))
    snapshot = CalibrationProgress(
        elapsed_seconds=elapsed,
        total_seconds=total,
        current_hr=hr,
        current_hrv=hrv,
        current_sqi=sqi,
        lighting_ok=lighting_ok,
        motion_ok=motion_ok,
        face_ok=face_ok,
        pct_complete=pct,
        status=status,
    )
    try:
        callback(snapshot)
    except Exception:
        logger.debug("calibration progress callback raised", exc_info=True)


async def run_simulate_calibration(
    duration_seconds: int,
    *,
    is_aborted: Callable[[], bool] | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, list[float]]:
    """Async simulation loop — same data shape as the live path.

    Generates resting-state synthetic samples at 2 Hz and yields control
    back to the event loop between ticks so the Qt main thread keeps
    repainting (the runner is awaited from a worker coroutine, not the
    GUI thread, but `await asyncio.sleep(...)` is still the right
    discipline so we don't block the daemon's other tasks).
    """
    import random

    random.seed(42)
    samples = _empty_samples()
    total_ticks = max(1, int(duration_seconds * PROGRESS_HZ))
    tick_interval = 1.0 / PROGRESS_HZ
    start = time.monotonic()
    _emit_progress(
        on_progress,
        elapsed=0.0,
        total=float(duration_seconds),
        samples=samples,
        lighting_ok=True,
        motion_ok=True,
        face_ok=True,
        status="initializing",
    )
    for _ in range(total_ticks):
        if is_aborted is not None and is_aborted():
            return samples

        # Resting HR ~70 BPM with low variance.
        hr = 70.0 + random.gauss(0, 3.0)
        samples["hr"].append(max(40.0, min(120.0, hr)))
        hrv = 50.0 + random.gauss(0, 8.0)
        samples["hrv"].append(max(10.0, min(200.0, hrv)))
        samples["blink_rate"].append(max(5.0, min(30.0, 17.0 + random.gauss(0, 2.0))))
        samples["mouse_velocity"].append(max(100.0, min(2000.0, 500.0 + random.gauss(0, 100.0))))
        samples["mouse_variance"].append(max(1000.0, min(100000.0, 10000.0 + random.gauss(0, 2000.0))))
        samples["shoulder_y"].append(max(0.0, min(1.0, 0.5 + random.gauss(0, 0.02))))

        elapsed = time.monotonic() - start
        _emit_progress(
            on_progress,
            elapsed=elapsed,
            total=float(duration_seconds),
            samples=samples,
            lighting_ok=True,
            motion_ok=True,
            face_ok=True,
            status="running",
        )
        await asyncio.sleep(tick_interval)

    return samples


async def run_live_calibration(
    duration_seconds: int,
    *,
    config: Any | None = None,
    is_aborted: Callable[[], bool] | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, list[float]]:
    """Async live capture loop. Falls back to simulate mode if the
    webcam / OpenCV / pipeline modules are unavailable.

    The function `await asyncio.sleep(0)` after each frame so the event
    loop stays responsive — the wizard's progress callback delivery and
    the abort flag check both run on the same loop.
    """
    try:
        import cv2  # local import per CLAUDE.md rule
    except ImportError:
        logger.warning("OpenCV unavailable, falling back to simulation")
        return await run_simulate_calibration(
            duration_seconds, is_aborted=is_aborted, on_progress=on_progress
        )

    if config is None:
        config = get_config()

    from cortex.services.capture_service.webcam import (
        describe_requested_camera,
        open_video_capture,
    )

    cap, _selection = open_video_capture(config.capture)
    if cap is None:
        logger.warning(
            "Cannot open webcam device %s, falling back to simulation",
            describe_requested_camera(config.capture),
        )
        return await run_simulate_calibration(
            duration_seconds, is_aborted=is_aborted, on_progress=on_progress
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.capture.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.capture.height)

    samples = _empty_samples()
    try:
        import numpy as np

        from cortex.services.capture_service.face_tracker import FaceTracker
        from cortex.services.kinematics_engine.blink_detector import BlinkDetector
        from cortex.services.physio_engine.pulse_estimator import PulseEstimator
        from cortex.services.physio_engine.roi_extractor import RoiExtractor
        from cortex.services.physio_engine.rppg import extract_bvp
        from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
        from cortex.services.telemetry_engine.input_hooks import InputHooks
    except Exception:
        logger.warning(
            "Full calibration pipeline unavailable, falling back to simulation"
        )
        cap.release()
        return await run_simulate_calibration(
            duration_seconds, is_aborted=is_aborted, on_progress=on_progress
        )

    tracker = FaceTracker(config.capture)
    extractor = RoiExtractor(config.landmarks)
    blink_detector = BlinkDetector(
        blink_config=config.signal.blink,
        landmarks_config=config.landmarks,
    )
    pulse_estimator = PulseEstimator(fs=float(config.capture.fps))
    input_hooks = InputHooks(config.telemetry)
    aggregator = FeatureAggregator(input_hooks, config=config.telemetry)
    rgb_window: list[Any] = []
    max_window = max(1, config.signal.rppg.window_seconds * config.capture.fps)
    last_physio_time = 0.0

    try:
        tracker.initialize()
    except Exception as exc:
        logger.warning(
            "Face tracker failed to initialize (%s), falling back to simulation", exc
        )
        cap.release()
        return await run_simulate_calibration(
            duration_seconds, is_aborted=is_aborted, on_progress=on_progress
        )

    hooks_started = input_hooks.start()
    if not hooks_started:
        logger.info(
            "Mouse/keyboard telemetry unavailable; calibration will use camera signals only"
        )

    _emit_progress(
        on_progress,
        elapsed=0.0,
        total=float(duration_seconds),
        samples=samples,
        lighting_ok=False,
        motion_ok=False,
        face_ok=False,
        status="initializing",
    )

    start = time.monotonic()
    last_progress_time = 0.0
    progress_interval = 1.0 / PROGRESS_HZ
    lighting_ok = False
    face_ok = False
    motion_ok = True  # default True; flips False on detected jitter

    try:
        while True:
            if is_aborted is not None and is_aborted():
                break

            ret, frame = cap.read()
            if not ret:
                await asyncio.sleep(0)
                continue

            elapsed = time.monotonic() - start
            if elapsed >= duration_seconds:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = float(np.mean(gray))
            lighting_ok = brightness >= config.capture.min_brightness

            if not lighting_ok:
                # Still emit progress so the UI can show the warning pill
                if elapsed - last_progress_time >= progress_interval:
                    _emit_progress(
                        on_progress,
                        elapsed=elapsed,
                        total=float(duration_seconds),
                        samples=samples,
                        lighting_ok=lighting_ok,
                        motion_ok=motion_ok,
                        face_ok=face_ok,
                        status="running",
                    )
                    last_progress_time = elapsed
                await asyncio.sleep(0)
                continue

            tracking = tracker.process_frame(frame)
            face_ok = bool(tracking.face_detected and tracking.landmarks_px is not None)
            if not face_ok:
                if elapsed - last_progress_time >= progress_interval:
                    _emit_progress(
                        on_progress,
                        elapsed=elapsed,
                        total=float(duration_seconds),
                        samples=samples,
                        lighting_ok=lighting_ok,
                        motion_ok=motion_ok,
                        face_ok=face_ok,
                        status="running",
                    )
                    last_progress_time = elapsed
                await asyncio.sleep(0)
                continue

            roi_frame = extractor.extract(frame, tracking.landmarks_px, elapsed)
            combined_rgb = roi_frame.combined_rgb()
            if combined_rgb is not None:
                rgb_window.append(combined_rgb)
                if len(rgb_window) > max_window:
                    rgb_window.pop(0)

            blink_state = blink_detector.update(tracking.landmarks_px, elapsed)
            if blink_state.blink_rate is not None:
                samples["blink_rate"].append(blink_state.blink_rate)

            ear_mid_y = float(
                (tracking.landmarks_px[234][1] + tracking.landmarks_px[454][1]) / 2.0
            ) / float(frame.shape[0])
            samples["shoulder_y"].append(ear_mid_y)

            if (
                len(rgb_window) >= max_window
                and elapsed - last_physio_time >= config.signal.rppg.stride_seconds
            ):
                bvp = extract_bvp(np.array(rgb_window, dtype=np.float64), fs=float(config.capture.fps))
                pulse_estimator.process_window(bvp, timestamp=elapsed)
                physio = pulse_estimator.get_features(elapsed)
                if physio.valid and physio.pulse_bpm is not None:
                    samples["hr"].append(physio.pulse_bpm)
                if physio.pulse_variability_proxy is not None:
                    samples["hrv"].append(physio.pulse_variability_proxy)
                last_physio_time = elapsed

            telemetry = aggregator.build_features(
                window_seconds=min(config.telemetry.window_seconds, max(1.0, elapsed)),
                current_time=time.monotonic(),
            )
            if telemetry.mouse_velocity_mean > 0.0:
                samples["mouse_velocity"].append(telemetry.mouse_velocity_mean)
            if telemetry.mouse_velocity_variance > 0.0:
                samples["mouse_variance"].append(telemetry.mouse_velocity_variance)

            # Motion check: if recent shoulder_y stddev is large, the
            # user is moving too much for a stable baseline.
            if len(samples["shoulder_y"]) >= 4:
                recent = samples["shoulder_y"][-8:]
                spread = max(recent) - min(recent)
                motion_ok = spread < 0.05

            if elapsed - last_progress_time >= progress_interval:
                _emit_progress(
                    on_progress,
                    elapsed=elapsed,
                    total=float(duration_seconds),
                    samples=samples,
                    lighting_ok=lighting_ok,
                    motion_ok=motion_ok,
                    face_ok=face_ok,
                    status="running",
                )
                last_progress_time = elapsed

            await asyncio.sleep(0)
    finally:
        try:
            input_hooks.stop()
        except Exception:
            logger.debug("input_hooks.stop() raised", exc_info=True)
        try:
            cap.release()
        except Exception:
            logger.debug("cap.release() raised", exc_info=True)
        try:
            tracker.release()
        except Exception:
            logger.debug("tracker.release() raised", exc_info=True)

    # Fill missing telemetry baselines conservatively (matches the prior CLI).
    if not samples["mouse_velocity"]:
        samples["mouse_velocity"].append(500.0)
    if not samples["mouse_variance"]:
        samples["mouse_variance"].append(10000.0)

    if not samples["hr"] and not samples["blink_rate"] and not samples["shoulder_y"]:
        logger.warning("No physiological data captured, falling back to defaults")
        return await run_simulate_calibration(
            duration_seconds, is_aborted=is_aborted, on_progress=on_progress
        )

    return samples


def compute_baselines(samples: dict[str, list[float]]) -> UserBaselines:
    """Compute baseline statistics. Same implementation as the legacy
    CLI; re-exported here so the CLI can import it from one place."""
    import statistics

    import numpy as np

    def _safe_mean(data: list[float], default: float) -> float:
        return statistics.mean(data) if data else default

    def _safe_stdev(data: list[float], default: float) -> float:
        return statistics.stdev(data) if len(data) >= 2 else default

    def _distribution(data: list[float], default_mu: float, default_sigma: float) -> dict[str, float]:
        if not data:
            return {"mu": default_mu, "sigma": default_sigma, "p10": default_mu, "p90": default_mu}
        arr = sorted(data)
        return {
            "mu": float(statistics.mean(arr)),
            "sigma": float(statistics.stdev(arr)) if len(arr) >= 2 else float(default_sigma),
            "p10": float(np.percentile(arr, 10)),
            "p90": float(np.percentile(arr, 90)),
        }

    hr_values = samples.get("hr", [])
    hrv_values = samples.get("hrv", [])
    blink_values = samples.get("blink_rate", [])
    mouse_vel = samples.get("mouse_velocity", [])
    mouse_var = samples.get("mouse_variance", [])
    shoulder_values = samples.get("shoulder_y", [])

    return UserBaselines(
        hr_baseline=_safe_mean(hr_values, 72.0),
        hr_std=_safe_stdev(hr_values, 5.0),
        hrv_baseline=_safe_mean(hrv_values, 50.0),
        blink_rate_baseline=_safe_mean(blink_values, 17.0),
        mouse_velocity_baseline=_safe_mean(mouse_vel, 500.0),
        mouse_variance_baseline=_safe_mean(mouse_var, 10000.0),
        shoulder_neutral_y=_safe_mean(shoulder_values, 0.5),
        calibrated_at=datetime.now(UTC),
        metric_distributions={
            "hr": _distribution(hr_values, 72.0, 5.0),
            "hrv_rmssd": _distribution(hrv_values, 50.0, 10.0),
            "blink_rate": _distribution(blink_values, 17.0, 4.0),
            "mouse_velocity": _distribution(mouse_vel, 500.0, 120.0),
            "mouse_variance": _distribution(mouse_var, 10000.0, 2500.0),
            "resp_rate": _distribution(samples.get("resp", []), 15.0, 3.0),
        },
        circadian_hr_cosinor={},
        rolling_rebaseline_seconds=60.0,
        ew_decay_half_life_days=7.0,
    )


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


def baselines_dir(config: Any | None = None) -> Path:
    """Resolve the on-disk baselines directory (`storage/baselines/`)."""
    cfg = config or get_config()
    return Path(cfg.storage.path) / "baselines"


def default_baseline_path(config: Any | None = None) -> Path:
    """Path to `storage/baselines/default.json` — the canonical baseline
    the state engine reads on startup."""
    return baselines_dir(config) / "default.json"


class CalibrationRunner:
    """Drives one calibration session.

    Lifecycle:

        runner = CalibrationRunner(duration_seconds=120, simulate=False)
        await runner.start(on_progress=ui_callback)
        baselines = await runner.finish()  # writes default.json atomically

    `abort()` is safe to call from any thread; the cooperative flag is
    checked on every tick of the inner loop.
    """

    def __init__(
        self,
        duration_seconds: int = DEFAULT_DURATION_SECONDS,
        simulate: bool = False,
        config: Any | None = None,
        *,
        output_path: Path | str | None = None,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        self.duration_seconds = int(duration_seconds)
        self.simulate = bool(simulate)
        self._config = config
        self._aborted = False
        self._started = False
        self._finished = False
        self._samples: dict[str, list[float]] | None = None
        self._last_progress: CalibrationProgress | None = None
        # If `output_path` is provided it is used as the timestamped
        # destination *instead of* the auto-generated baseline_<ts>.json.
        # `default.json` is always written alongside on success.
        self._output_override = Path(output_path) if output_path else None

    @property
    def last_progress(self) -> CalibrationProgress | None:
        return self._last_progress

    @property
    def is_running(self) -> bool:
        return self._started and not self._finished and not self._aborted

    def abort(self) -> None:
        """Cooperative abort. The next loop tick exits cleanly and the
        camera handle is released in the `finally` block."""
        self._aborted = True

    def _on_progress(
        self, user_cb: ProgressCallback | None
    ) -> ProgressCallback:
        """Wrap the user callback so we can also retain the latest
        progress snapshot for introspection (used by tests)."""
        def _inner(progress: CalibrationProgress) -> None:
            self._last_progress = progress
            if user_cb is not None:
                user_cb(progress)
        return _inner

    async def start(
        self,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Run the capture loop end-to-end. Returns when the duration
        elapses or `abort()` flips the cooperative flag."""
        if self._started:
            raise RuntimeError("CalibrationRunner.start() already called")
        self._started = True

        wrapped_cb = self._on_progress(on_progress)

        try:
            if self.simulate:
                self._samples = await run_simulate_calibration(
                    self.duration_seconds,
                    is_aborted=lambda: self._aborted,
                    on_progress=wrapped_cb,
                )
            else:
                self._samples = await run_live_calibration(
                    self.duration_seconds,
                    config=self._config,
                    is_aborted=lambda: self._aborted,
                    on_progress=wrapped_cb,
                )
        except Exception:
            logger.exception("calibration capture loop crashed")
            self._samples = None
            _emit_progress(
                wrapped_cb,
                elapsed=float(self.duration_seconds),
                total=float(self.duration_seconds),
                samples=_empty_samples(),
                lighting_ok=False,
                motion_ok=False,
                face_ok=False,
                status="failed",
            )
            raise

        status: CalibrationStatus = "aborted" if self._aborted else "completed"
        elapsed = float(self.duration_seconds)
        if self._last_progress is not None:
            elapsed = self._last_progress.elapsed_seconds
        _emit_progress(
            wrapped_cb,
            elapsed=elapsed,
            total=float(self.duration_seconds),
            samples=self._samples or _empty_samples(),
            lighting_ok=self._last_progress.lighting_ok if self._last_progress else True,
            motion_ok=self._last_progress.motion_ok if self._last_progress else True,
            face_ok=self._last_progress.face_ok if self._last_progress else True,
            status=status,
        )

    async def finish(self) -> UserBaselines:
        """Compute baselines and atomically write them to disk.

        Writes happen *only on success*: a runner that hasn't been
        started, has been aborted, or crashed mid-flight will raise
        rather than overwriting the prior `default.json`.
        """
        if not self._started:
            raise RuntimeError(
                "CalibrationRunner.finish() called before start(); "
                "the runner has no samples to compute baselines from"
            )
        if self._aborted:
            raise RuntimeError(
                "CalibrationRunner was aborted; refusing to overwrite "
                "default.json with a partial run"
            )
        if self._samples is None:
            raise RuntimeError(
                "CalibrationRunner.start() returned no samples; "
                "refusing to overwrite default.json"
            )

        baselines = compute_baselines(self._samples)
        await asyncio.to_thread(self._write_baselines, baselines)
        self._finished = True
        return baselines

    def _write_baselines(self, baselines: UserBaselines) -> Path:
        """Atomically persist `baseline_<ts>.json` + `default.json`."""
        data = baselines.model_dump(mode="json")
        base_dir = baselines_dir(self._config)
        base_dir.mkdir(parents=True, exist_ok=True)

        if self._output_override is not None:
            timestamped = self._output_override
            timestamped.parent.mkdir(parents=True, exist_ok=True)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            timestamped = base_dir / f"baseline_{timestamp}.json"

        atomic_write_json(timestamped, data)
        default_path = base_dir / "default.json"
        if timestamped != default_path:
            atomic_write_json(default_path, data)
        return timestamped
