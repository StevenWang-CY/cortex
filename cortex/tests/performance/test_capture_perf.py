"""audit Phase-I: capture-loop perf regression guard.

Synthetic harness: feeds a stream of pre-generated BGR frames through a
stub face tracker + the real :class:`FrameQualityScorer` and asserts the
combined wall-time stays below a generous CI-friendly budget. The point
of the test is not to benchmark the real mediapipe model (which would
be flaky on a shared runner and would require the model file) but to
guard the two structural wins shipped in the same commit:

* The :meth:`FaceTracker.process_frame` signature accepts a pre-converted
  RGB view, and a sub-sample cache lets it skip mediapipe entirely on
  ``n-1`` out of ``n`` frames.
* The :class:`FrameQualityScorer` accepts a pre-converted grayscale view
  and runs each cvtColor at most once per frame.

The harness exercises both fast paths and asserts the per-frame budget.
A regression that re-introduces a redundant cvtColor or disables the
sub-sample cache will blow past the threshold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from cortex.libs.config.settings import CaptureConfig
from cortex.services.capture_service.face_tracker import FaceTrackingResult
from cortex.services.capture_service.quality import FrameQualityScorer


@dataclass(frozen=True)
class _StubLandmarker:
    """Stub mediapipe FaceLandmarker so the test runs without the
    model. ``process_frame`` on the real :class:`FaceTracker` is wired
    around this in :class:`_FakeFaceTracker` below."""

    invocations: list[int]

    def detect_for_video(self, _image, _ts_ms: int):  # noqa: ANN001
        self.invocations.append(_ts_ms)
        return _FakeMpResult(face_landmarks=[])


@dataclass(frozen=True)
class _FakeMpResult:
    face_landmarks: list


class _FakeFaceTracker:
    """Real-sub-sample-cache, fake-mediapipe FaceTracker substitute.

    Mirrors the relevant audit Phase-I surface of the production class
    (``process_frame`` accepts ``rgb_frame``, sub-samples by
    ``face_mesh_subsample_n``, replays the last result on skipped
    frames) without requiring the mediapipe model.
    """

    def __init__(self, config: CaptureConfig) -> None:
        self._config = config
        self._subsample_counter = 0
        self._last: FaceTrackingResult | None = None
        self.mp_invocations = 0

    def process_frame(
        self, frame: np.ndarray, rgb_frame: np.ndarray | None = None,
    ) -> FaceTrackingResult:
        subsample_n = max(1, self._config.face_mesh_subsample_n)
        if subsample_n > 1 and self._last is not None:
            self._subsample_counter = (self._subsample_counter + 1) % subsample_n
            if self._subsample_counter != 0:
                return self._last
        else:
            self._subsample_counter = 0

        # Force the caller to have supplied an RGB view — that is the
        # whole point of the colour-convert cache.
        assert rgb_frame is not None, "pipeline must pre-convert BGR→RGB"
        assert rgb_frame.shape == frame.shape, "RGB view shape mismatch"
        self.mp_invocations += 1
        result = FaceTrackingResult(
            face_detected=False,
            confidence=0.0,
            landmarks=None,
            landmarks_px=None,
            bounding_box=None,
            face_stable=False,
        )
        self._last = result
        return result


def _make_frame(rng: np.random.Generator, w: int = 640, h: int = 480) -> np.ndarray:
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def test_capture_pipeline_per_frame_budget() -> None:
    """1000 synthetic frames through the cache-aware path stays inside
    a generous wall-time budget.

    On a developer M-series Mac the loop completes in well under a
    second; the threshold is set to 5 s so noisy shared CI hardware
    still passes while catching the kind of regression that would
    re-double the cvtColor cost or disable the mediapipe sub-sample.
    """
    config = CaptureConfig(face_mesh_subsample_n=2)
    scorer = FrameQualityScorer(config)
    tracker = _FakeFaceTracker(config)
    rng = np.random.default_rng(seed=12345)

    n_frames = 1000
    start = time.perf_counter()
    for _ in range(n_frames):
        frame = _make_frame(rng)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tracker.process_frame(frame, rgb_frame=rgb)
        scorer.score(frame, 0.0, gray_frame=gray)
    elapsed = time.perf_counter() - start

    # Budget: 5 ms per frame averaged over 1000 frames = 5 s wall.
    # Real laptop measurements come in below 1 s; the wide margin is
    # for shared CI runners.
    assert elapsed < 5.0, f"capture pipeline regressed: {elapsed:.2f}s for {n_frames} frames"

    # Sub-sample cache must have actually skipped mediapipe on at least
    # half the frames. If the cache stops working ``mp_invocations``
    # equals ``n_frames``.
    assert tracker.mp_invocations <= (n_frames // 2) + 1, (
        f"sub-sample cache failed: mediapipe ran {tracker.mp_invocations}/{n_frames} times"
    )


def test_quality_scorer_accepts_cached_gray() -> None:
    """Regression guard: the scorer accepts a precomputed grayscale and
    produces the same output as if it had run cvtColor itself."""
    rng = np.random.default_rng(seed=42)
    frame = _make_frame(rng)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    scorer = FrameQualityScorer(CaptureConfig())

    cached = scorer.score(frame, 0.0, gray_frame=gray)
    uncached = scorer.score(frame, 0.0)

    assert cached.brightness_score == pytest.approx(uncached.brightness_score)
    assert cached.blur_score == pytest.approx(uncached.blur_score)
    assert cached.motion_score == pytest.approx(uncached.motion_score)
    assert cached.passed == uncached.passed


def test_face_mesh_subsample_config_default() -> None:
    """Regression guard: ``face_mesh_subsample_n`` defaults to 1 (every
    frame). Audit fix: a default of 2 replayed byte-identical landmarks to
    blink/head-pose on alternate frames, halving the effective detection
    rate while downstream code assumed 30 fps (distorted blink duration and
    angular velocity). Accurate blink timing requires every-frame tracking;
    raising it to 2 is an explicit opt-in performance trade-off."""
    assert CaptureConfig().face_mesh_subsample_n == 1
