"""
Cortex Capture Test — Standalone Webcam Preview

Launches the webcam capture pipeline and displays annotated frames
with face detection bounding box, quality metrics, FPS counter, and
landmark visualization.

Usage:
    python -m cortex.scripts.run_capture
    cortex-capture  # if installed via pip
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from cortex.libs.config.settings import CaptureConfig, get_config
from cortex.services.capture_service.webcam import (
    describe_requested_camera,
    open_video_capture,
)

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cortex webcam capture test",
    )
    parser.add_argument(
        "--device", "-d", type=int, default=None,
        help="Webcam device ID (default: from config)",
    )
    parser.add_argument(
        "--fps", type=int, default=None,
        help="Target FPS (default: from config)",
    )
    parser.add_argument(
        "--width", type=int, default=None,
        help="Frame width (default: from config)",
    )
    parser.add_argument(
        "--height", type=int, default=None,
        help="Frame height (default: from config)",
    )
    parser.add_argument(
        "--no-display", action="store_true",
        help="Run without display (headless mode for testing)",
    )
    parser.add_argument(
        "--duration", type=float, default=0,
        help="Run for N seconds then exit (0 = run until Ctrl+C)",
    )
    return parser.parse_args()


def _run_capture_loop(
    config: CaptureConfig,
    *,
    display: bool = True,
    duration: float = 0,
) -> dict[str, float]:
    """
    Run the capture loop.

    Returns statistics dict with fps_mean, fps_min, fps_max,
    frames_total, face_detection_rate, quality_pass_rate.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("ERROR: opencv-python is required. Install with: pip install opencv-python")
        sys.exit(1)

    cap, selection = open_video_capture(config)
    if cap is None:
        print(f"ERROR: Cannot open webcam device {describe_requested_camera(config)}")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.height)

    # Statistics tracking
    frame_count = 0
    face_count = 0
    quality_pass_count = 0
    fps_samples: list[float] = []
    start_time = time.monotonic()
    last_frame_time = start_time

    selected_device = selection.device_id if selection is not None else describe_requested_camera(config)
    selected_name = f" ({selection.device_name})" if selection and selection.device_name else ""
    print(
        f"Capture started: device={selected_device}{selected_name} "
        f"resolution={config.width}x{config.height} fps={config.fps}"
    )
    if display:
        print("Press 'q' to quit")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Failed to capture frame")
                continue

            now = time.monotonic()
            dt = now - last_frame_time
            last_frame_time = now
            frame_count += 1

            # Compute instantaneous FPS
            if dt > 0:
                inst_fps = 1.0 / dt
                fps_samples.append(inst_fps)

            # Quality metrics (simple, no MediaPipe dependency needed)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = float(np.mean(gray))
            blur_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

            brightness_ok = brightness >= config.min_brightness
            blur_ok = blur_var >= 50.0  # reasonable sharpness threshold
            quality_ok = brightness_ok and blur_ok
            if quality_ok:
                quality_pass_count += 1

            # Simple face detection using Haar cascade (lightweight)
            face_detected = False
            try:
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                face_cascade = cv2.CascadeClassifier(cascade_path)
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
                )
                face_detected = len(faces) > 0
                if face_detected:
                    face_count += 1
            except Exception:
                pass

            # Annotate frame if display is on
            if display:
                # FPS counter
                fps_text = f"FPS: {inst_fps:.1f}" if dt > 0 else "FPS: --"
                cv2.putText(frame, fps_text, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                # Quality metrics
                bright_color = (0, 255, 0) if brightness_ok else (0, 0, 255)
                cv2.putText(frame, f"Bright: {brightness:.0f}", (10, 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, bright_color, 1)

                blur_color = (0, 255, 0) if blur_ok else (0, 0, 255)
                cv2.putText(frame, f"Sharp: {blur_var:.0f}", (10, 85),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, blur_color, 1)

                # Face detection indicator
                face_color = (0, 255, 0) if face_detected else (0, 0, 255)
                face_text = "Face: YES" if face_detected else "Face: NO"
                cv2.putText(frame, face_text, (10, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, face_color, 1)

                # Draw face bounding boxes
                if face_detected:
                    for (x, y, w, h) in faces:
                        cv2.rectangle(frame, (x, y), (x + w, y + h),
                                      (0, 255, 0), 2)

                # Frame counter
                elapsed = now - start_time
                cv2.putText(
                    frame,
                    f"Frames: {frame_count} | Time: {elapsed:.1f}s",
                    (10, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1,
                )

                cv2.imshow("Cortex Capture Test", frame)

                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break

            # Duration check
            elapsed = now - start_time
            if duration > 0 and elapsed >= duration:
                break

            # FPS regulation
            target_dt = 1.0 / config.fps
            sleep_time = target_dt - (time.monotonic() - now)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if display:
            cv2.destroyAllWindows()

    # Compute statistics
    total_time = time.monotonic() - start_time
    stats: dict[str, float] = {
        "frames_total": float(frame_count),
        "duration_seconds": total_time,
        "fps_mean": float(frame_count / total_time) if total_time > 0 else 0.0,
        "fps_min": float(min(fps_samples)) if fps_samples else 0.0,
        "fps_max": float(max(fps_samples)) if fps_samples else 0.0,
        "face_detection_rate": (
            float(face_count / frame_count) if frame_count > 0 else 0.0
        ),
        "quality_pass_rate": (
            float(quality_pass_count / frame_count) if frame_count > 0 else 0.0
        ),
    }
    return stats


def main() -> None:
    """Entry point for cortex-capture command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = _parse_args()
    config = get_config()
    cap_config = config.capture

    # Override config with CLI args
    if args.device is not None:
        cap_config = cap_config.model_copy(update={"device_id": args.device})
    if args.fps is not None:
        cap_config = cap_config.model_copy(update={"fps": args.fps})
    if args.width is not None:
        cap_config = cap_config.model_copy(update={"width": args.width})
    if args.height is not None:
        cap_config = cap_config.model_copy(update={"height": args.height})

    print("=" * 50)
    print("  Cortex Capture Test")
    print("=" * 50)
    print(f"  Device:     {describe_requested_camera(cap_config)}")
    print(f"  Resolution: {cap_config.width}x{cap_config.height}")
    print(f"  Target FPS: {cap_config.fps}")
    print(f"  Display:    {not args.no_display}")
    if args.duration > 0:
        print(f"  Duration:   {args.duration}s")
    print("=" * 50)

    stats = _run_capture_loop(
        cap_config,
        display=not args.no_display,
        duration=args.duration,
    )

    print("\n--- Capture Statistics ---")
    print(f"  Total Frames:       {stats['frames_total']:.0f}")
    print(f"  Duration:           {stats['duration_seconds']:.1f}s")
    print(f"  FPS (mean):         {stats['fps_mean']:.1f}")
    print(f"  FPS (min/max):      {stats['fps_min']:.1f} / {stats['fps_max']:.1f}")
    print(f"  Face Detection:     {stats['face_detection_rate']:.1%}")
    print(f"  Quality Pass Rate:  {stats['quality_pass_rate']:.1%}")


if __name__ == "__main__":
    main()
