"""
Cortex Calibration — 2-Minute Baseline Capture

Captures personal baselines for heart rate, HRV, blink rate, posture,
and mouse velocity. The user sits calmly while the system records
physiological and behavioral signals to establish individual norms.

Outputs a JSON baseline profile saved to storage/baselines/.

Usage:
    python -m cortex.scripts.calibrate
    cortex-calibrate  # if installed via pip
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cortex.libs.config.settings import get_config
from cortex.libs.schemas.state import UserBaselines

logger = logging.getLogger(__name__)

# Default calibration duration
DEFAULT_DURATION_SECONDS = 120  # 2 minutes


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cortex calibration — capture personal baselines",
    )
    parser.add_argument(
        "--duration", "-d", type=int, default=DEFAULT_DURATION_SECONDS,
        help=f"Calibration duration in seconds (default: {DEFAULT_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file path (default: storage/baselines/baseline_<timestamp>.json)",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Simulate calibration with synthetic data (no webcam needed)",
    )
    return parser.parse_args()


def _simulate_calibration(
    duration_seconds: int,
) -> dict[str, list[float]]:
    """
    Simulate a calibration session with synthetic physiological data.

    Generates realistic resting-state values:
    - HR: ~70 BPM with natural variability
    - HRV (RMSSD): ~50ms
    - Blink rate: ~17/min
    - Mouse velocity: ~500 px/s at rest
    """
    import random

    random.seed(42)  # Reproducible for testing

    samples: dict[str, list[float]] = {
        "hr": [],
        "hrv": [],
        "blink_rate": [],
        "mouse_velocity": [],
        "mouse_variance": [],
        "shoulder_y": [],
    }

    # Simulate sampling at ~2 Hz
    num_samples = duration_seconds * 2
    print(f"\nSimulating {duration_seconds}s calibration ({num_samples} samples)...")

    for i in range(num_samples):
        # Resting HR: ~70 BPM, std ~5
        hr = 70.0 + random.gauss(0, 3.0)
        samples["hr"].append(max(40.0, min(120.0, hr)))

        # Resting HRV (RMSSD): ~50ms, std ~10
        hrv = 50.0 + random.gauss(0, 8.0)
        samples["hrv"].append(max(10.0, min(200.0, hrv)))

        # Resting blink rate: ~17/min, std ~3
        br = 17.0 + random.gauss(0, 2.0)
        samples["blink_rate"].append(max(5.0, min(30.0, br)))

        # Resting mouse velocity: ~500 px/s
        mv = 500.0 + random.gauss(0, 100.0)
        samples["mouse_velocity"].append(max(100.0, min(2000.0, mv)))

        # Mouse variance
        mvv = 10000.0 + random.gauss(0, 2000.0)
        samples["mouse_variance"].append(max(1000.0, min(100000.0, mvv)))

        # Shoulder Y position (normalized 0-1): ~0.5
        sy = 0.5 + random.gauss(0, 0.02)
        samples["shoulder_y"].append(max(0.0, min(1.0, sy)))

        # Progress indicator
        elapsed = (i + 1) / 2.0
        if (i + 1) % 20 == 0 or i == num_samples - 1:
            pct = (i + 1) / num_samples * 100
            print(f"  [{pct:5.1f}%] {elapsed:.0f}s / {duration_seconds}s")

        time.sleep(0.01)  # Minimal delay for realism

    return samples


def _live_calibration(
    duration_seconds: int,
) -> dict[str, list[float]]:
    """
    Run live calibration using the webcam.

    Falls back to simulation if webcam is unavailable.
    """
    try:
        import cv2
    except ImportError:
        print("WARNING: OpenCV not available, falling back to simulation")
        return _simulate_calibration(duration_seconds)

    config = get_config()
    from cortex.services.capture_service.webcam import (
        describe_requested_camera,
        open_video_capture,
    )

    cap, _selection = open_video_capture(config.capture)
    if cap is None:
        print(
            f"WARNING: Cannot open webcam device {describe_requested_camera(config.capture)}, "
            "falling back to simulation"
        )
        return _simulate_calibration(duration_seconds)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.capture.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.capture.height)

    samples: dict[str, list[float]] = {
        "hr": [],
        "hrv": [],
        "blink_rate": [],
        "mouse_velocity": [],
        "mouse_variance": [],
        "shoulder_y": [],
    }

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
        print("WARNING: Full calibration pipeline unavailable, falling back to simulation")
        return _simulate_calibration(duration_seconds)

    tracker = FaceTracker(config.capture)
    extractor = RoiExtractor(config.landmarks)
    blink_detector = BlinkDetector(
        blink_config=config.signal.blink,
        landmarks_config=config.landmarks,
    )
    pulse_estimator = PulseEstimator(fs=float(config.capture.fps))
    input_hooks = InputHooks(config.telemetry)
    aggregator = FeatureAggregator(input_hooks, config=config.telemetry)
    rgb_window: list[np.ndarray] = []
    max_window = max(1, config.signal.rppg.window_seconds * config.capture.fps)
    last_physio_time = 0.0

    try:
        tracker.initialize()
    except Exception as exc:
        print(f"WARNING: Face tracker failed to initialize ({exc}), falling back to simulation")
        return _simulate_calibration(duration_seconds)

    print(f"\nCalibrating for {duration_seconds}s — please sit calmly and look at the screen")
    print("Press 'q' to abort\n")

    start = time.monotonic()
    frame_count = 0
    hooks_started = input_hooks.start()
    if not hooks_started:
        print("INFO: Mouse/keyboard telemetry unavailable; calibration will use camera signals only")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                continue

            frame_count += 1
            elapsed = time.monotonic() - start

            if elapsed >= duration_seconds:
                break

            # Progress every 10 seconds
            if frame_count % (config.capture.fps * 10) == 0:
                pct = elapsed / duration_seconds * 100
                print(f"  [{pct:5.1f}%] {elapsed:.0f}s / {duration_seconds}s "
                      f"({frame_count} frames)")

            # Basic frame quality check
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = float(np.mean(gray))

            if brightness < config.capture.min_brightness:
                continue  # Skip low-quality frames

            tracking = tracker.process_frame(frame)
            if not tracking.face_detected or tracking.landmarks_px is None:
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

            # Approximate neutral shoulder baseline from ear midpoint.
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

            # Check for quit
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("\nCalibration aborted by user")
                input_hooks.stop()
                cap.release()
                cv2.destroyAllWindows()
                sys.exit(1)

    finally:
        input_hooks.stop()
        cap.release()
        cv2.destroyAllWindows()
        tracker.release()

    # Fill missing telemetry baselines conservatively instead of discarding real camera data.
    if not samples["mouse_velocity"]:
        samples["mouse_velocity"].append(500.0)
    if not samples["mouse_variance"]:
        samples["mouse_variance"].append(10000.0)

    # If the signal path failed entirely, fall back gracefully.
    if not samples["hr"] and not samples["blink_rate"] and not samples["shoulder_y"]:
        print("WARNING: No physiological or blink data captured, using defaults")
        return _simulate_calibration(duration_seconds)

    return samples


def compute_baselines(
    samples: dict[str, list[float]],
) -> UserBaselines:
    """Compute baseline statistics from collected samples."""

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

    baselines = UserBaselines(
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

    return baselines


def save_baselines(
    baselines: UserBaselines,
    output_path: str | None = None,
) -> Path:
    """Save baselines to a JSON file."""
    config = get_config()

    if output_path:
        path = Path(output_path)
    else:
        base_dir = Path(config.storage.path) / "baselines"
        base_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = base_dir / f"baseline_{timestamp}.json"

    path.parent.mkdir(parents=True, exist_ok=True)

    data = baselines.model_dump(mode="json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    default_path = path.parent / "default.json"
    if path != default_path:
        with open(default_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    return path


def main() -> None:
    """Entry point for cortex-calibrate command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = _parse_args()

    print("=" * 50)
    print("  Cortex Calibration")
    print("=" * 50)
    print(f"  Duration: {args.duration}s")
    print(f"  Mode:     {'simulation' if args.simulate else 'live'}")
    print("=" * 50)

    if args.simulate:
        samples = _simulate_calibration(args.duration)
    else:
        samples = _live_calibration(args.duration)

    # Compute baselines
    baselines = compute_baselines(samples)

    # Display results
    print("\n--- Calibration Results ---")
    print(f"  Heart Rate:       {baselines.hr_baseline:.1f} BPM "
          f"(std: {baselines.hr_std:.1f})")
    print(f"  HRV (RMSSD):      {baselines.hrv_baseline:.1f} ms")
    print(f"  Blink Rate:       {baselines.blink_rate_baseline:.1f} /min")
    print(f"  Mouse Velocity:   {baselines.mouse_velocity_baseline:.0f} px/s")
    print(f"  Mouse Variance:   {baselines.mouse_variance_baseline:.0f}")
    print(f"  Shoulder Y:       {baselines.shoulder_neutral_y:.3f}")
    print(f"  Calibrated At:    {baselines.calibrated_at}")

    # Save
    path = save_baselines(baselines, args.output)
    print(f"\nBaseline saved to: {path}")


if __name__ == "__main__":
    main()
