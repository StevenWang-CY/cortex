"""Exercise the real MediaPipe wheel/model and its reviewed Intel boundary."""

from __future__ import annotations

import platform
import sys
from importlib.metadata import version
from pathlib import Path

import mediapipe
import numpy as np

from cortex.services.capture_service.face_tracker import FaceTracker


def test_real_face_landmarker_model_and_protobuf_boundary() -> None:
    """The native graph loads, runs, and never imports the vulnerable JSON API."""

    if sys.platform == "darwin" and platform.machine() == "x86_64":
        assert version("mediapipe") == "0.10.21"
        assert version("protobuf") == "4.25.9"

    package_root = Path(mediapipe.__file__).resolve().parent
    forbidden: list[str] = []
    for source in package_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8", errors="ignore")
        if "google.protobuf.json_format" in text or "ParseDict" in text:
            forbidden.append(str(source.relative_to(package_root)))
    assert forbidden == [], (
        "MediaPipe reached the protobuf JSON parser covered by the Intel audit "
        f"exception: {forbidden}"
    )

    tracker = FaceTracker()
    tracker.initialize()
    try:
        result = tracker.process_frame(
            np.zeros((480, 640, 3), dtype=np.uint8),
            capture_mono_ns=1_000_000_000,
        )
        assert result.face_detected is False
    finally:
        tracker.release()
