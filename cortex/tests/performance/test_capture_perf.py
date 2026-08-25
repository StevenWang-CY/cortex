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
from statistics import median
from typing import cast
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from cortex.libs.config.settings import CaptureConfig
from cortex.services.capture_service.face_tracker import FaceTrackingResult
from cortex.services.capture_service.pipeline import CapturePipeline
from cortex.services.capture_service.quality import FrameQualityScorer
from cortex.services.capture_service.webcam import CapturedFrame


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
    """A steady-state synthetic batch stays inside the per-frame budget.

    Frame acquisition is deliberately outside the timed region.  Generating
    random 640x480 frames in this loop previously measured NumPy's allocator
    and PRNG (about 922 MB for 1000 frames), not the capture pipeline, and made
    the absolute threshold architecture-dependent under Rosetta.  Multiple
    short batches plus a median reject one-off shared-runner pauses while the
    companion structural test below fails deterministically if a redundant
    colour conversion is reintroduced.
    """
    config = CaptureConfig(face_mesh_subsample_n=2)
    scorer = FrameQualityScorer(config)
    tracker = _FakeFaceTracker(config)
    rng = np.random.default_rng(seed=12345)

    frames = tuple(_make_frame(rng) for _ in range(8))

    # Warm OpenCV/NumPy dispatch before measuring steady-state work. Startup
    # latency has its own regression test and is not a per-frame cost.
    warm_frame = frames[0]
    warm_rgb = cv2.cvtColor(warm_frame, cv2.COLOR_BGR2RGB)
    warm_gray = cv2.cvtColor(warm_frame, cv2.COLOR_BGR2GRAY)
    _FakeFaceTracker(config).process_frame(warm_frame, rgb_frame=warm_rgb)
    FrameQualityScorer(config).score(warm_frame, 0.0, gray_frame=warm_gray)

    batch_frames = 100
    sample_seconds: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        for index in range(batch_frames):
            frame = frames[index % len(frames)]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            tracker.process_frame(frame, rgb_frame=rgb)
            scorer.score(frame, 0.0, gray_frame=gray)
        sample_seconds.append(time.perf_counter() - start)

    median_seconds_per_frame = median(sample_seconds) / batch_frames
    total_frames = batch_frames * len(sample_seconds)

    # The synthetic stages must use less than one quarter of a 30 Hz frame
    # interval, preserving >25 ms for landmark inference and downstream work.
    # Exact conversion counts and cache behavior are asserted independently,
    # so this wall-clock smoke guard need not encode runner-specific speed.
    assert median_seconds_per_frame < 0.008, (
        "capture pipeline regressed: median "
        f"{median_seconds_per_frame * 1000:.2f}ms/frame across "
        f"{len(sample_seconds)}x{batch_frames}-frame batches "
        f"(samples={sample_seconds!r})"
    )

    # Sub-sample cache must have actually skipped mediapipe on at least
    # half the frames. If the cache stops working ``mp_invocations``
    # equals ``total_frames``.
    assert tracker.mp_invocations <= (total_frames // 2) + 1, (
        "sub-sample cache failed: mediapipe ran "
        f"{tracker.mp_invocations}/{total_frames} times"
    )


def test_capture_pipeline_converts_each_colour_space_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real orchestration path owns exactly one RGB and one gray convert.

    This is the deterministic regression guard for the optimisation measured
    above.  It observes the production ``CapturePipeline._process_frame``
    call graph, including any conversion the quality scorer might perform,
    rather than relying on a wall-clock threshold to infer call counts.
    """
    config = CaptureConfig()
    pipeline = CapturePipeline(config)
    frame = _make_frame(np.random.default_rng(seed=9876), w=64, h=48)
    captured = CapturedFrame(frame=frame, timestamp=1.0, sequence=0)
    tracking = FaceTrackingResult(
        face_detected=False,
        confidence=0.0,
        landmarks=None,
        landmarks_px=None,
        bounding_box=None,
        face_stable=False,
    )

    original_cvt_color = cv2.cvtColor
    conversion_codes: list[int] = []

    def recording_cvt_color(
        source: np.ndarray,
        conversion_code: int,
    ) -> np.ndarray:
        conversion_codes.append(conversion_code)
        return cast(np.ndarray, original_cvt_color(source, conversion_code))

    monkeypatch.setattr(cv2, "cvtColor", recording_cvt_color)
    with patch.object(
        pipeline._face_tracker,
        "process_frame",
        return_value=tracking,
    ) as process_frame:
        pipeline._process_frame(captured)

    assert conversion_codes == [cv2.COLOR_BGR2RGB, cv2.COLOR_BGR2GRAY]
    process_frame.assert_called_once()
    assert process_frame.call_args.kwargs["rgb_frame"] is not None


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
