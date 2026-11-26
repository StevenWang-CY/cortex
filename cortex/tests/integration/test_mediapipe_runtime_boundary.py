"""Exercise the real MediaPipe wheel/model and its Intel Protobuf boundary.

MediaPipe 0.10.21, the last release with a macOS Intel wheel, declares
``protobuf<5``.  The project restates that release's requirements so Intel
resolves the patched 5.x series instead (PYSEC-2026-1805 has no 4.x fix).
This test is the evidence that the combination works: on Intel it loads the
bundled FaceLandmarker model and runs a frame through it with that Protobuf.
"""

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
        protobuf_release = tuple(int(part) for part in version("protobuf").split(".")[:3])
        assert (5, 29, 6) <= protobuf_release < (6, 0, 0), (
            "Intel must resolve the patched Protobuf 5.x series; "
            f"found {version('protobuf')}"
        )

    package_root = Path(mediapipe.__file__).resolve().parent
    forbidden: list[str] = []
    for source in package_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8", errors="ignore")
        if "google.protobuf.json_format" in text or "ParseDict" in text:
            forbidden.append(str(source.relative_to(package_root)))
    assert forbidden == [], (
        "MediaPipe reached the Protobuf JSON parser that PYSEC-2026-1805 "
        f"concerns: {forbidden}"
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
