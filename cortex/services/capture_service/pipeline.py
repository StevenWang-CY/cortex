"""
Capture Service — Integrated Capture Pipeline

Wires together WebcamCapture → FaceTracker → FrameQualityScorer and publishes
quality-gated FrameMeta + landmarks to an async output queue.

Features:
- Full webcam → face → quality pipeline
- Adaptive frame skip when processing falls behind
- Quality-gated output (only high-quality frames are forwarded)
- Structured output for downstream consumers (physio, kinematics)
- Graceful lifecycle management

Privacy: No frames are ever saved to disk. All processing is ephemeral.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from cortex.libs.config.settings import CaptureConfig, get_config
from cortex.libs.schemas.features import FrameMeta
from cortex.services.capture_service.face_tracker import FaceTracker, FaceTrackingResult
from cortex.services.capture_service.quality import FrameQuality, FrameQualityScorer
from cortex.services.capture_service.webcam import CapturedFrame, WebcamCapture

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineOutput:
    """Output of the capture pipeline for downstream consumers."""

    frame_meta: FrameMeta
    landmarks: np.ndarray | None  # (468, 3) normalized, or None
    landmarks_px: np.ndarray | None  # (468, 2) pixel coords, or None
    frame: np.ndarray  # BGR image (kept in memory only, never saved)
    quality: FrameQuality
    tracking: FaceTrackingResult


class AdaptiveFrameSkipper:
    """
    Adaptive frame skip logic.

    When processing falls behind real-time, skips frames to catch up.
    Tracks processing latency and adjusts skip rate dynamically.
    """

    def __init__(self, target_fps: int = 30) -> None:
        self._target_interval = 1.0 / target_fps
        self._last_process_time = 0.0
        self._processing_latency_ema = 0.0
        self._ema_alpha = 0.2
        self._skip_count = 0
        self._total_skipped = 0

    @property
    def total_skipped(self) -> int:
        """Total frames skipped since start."""
        return self._total_skipped

    @property
    def current_skip_rate(self) -> int:
        """Current number of frames to skip between processed frames."""
        return self._skip_count

    def update_latency(self, processing_time: float) -> None:
        """
        Update processing latency estimate.

        Args:
            processing_time: Time taken to process the last frame (seconds).
        """
        self._processing_latency_ema = (
            self._ema_alpha * processing_time
            + (1 - self._ema_alpha) * self._processing_latency_ema
        )

        # If processing takes longer than the frame interval, increase skip
        if self._processing_latency_ema > self._target_interval * 1.5:
            # Skip enough frames to keep up
            self._skip_count = min(
                5,  # Cap at skipping 5 frames
                int(self._processing_latency_ema / self._target_interval),
            )
        elif self._processing_latency_ema < self._target_interval * 0.8:
            # Processing is fast enough, reduce skipping
            self._skip_count = max(0, self._skip_count - 1)

    def should_skip(self, frame_sequence: int) -> bool:
        """
        Determine if a frame should be skipped.

        Args:
            frame_sequence: Frame sequence number.

        Returns:
            True if the frame should be skipped.
        """
        if self._skip_count == 0:
            return False

        # Skip frames based on modular arithmetic
        if frame_sequence % (self._skip_count + 1) != 0:
            self._total_skipped += 1
            return True
        return False


class CapturePipeline:
    """
    Integrated capture pipeline.

    Orchestrates: WebcamCapture → FaceTracker → QualityScorer → Output Queue

    Usage:
        pipeline = CapturePipeline()
        await pipeline.start()

        async for output in pipeline:
            process(output.frame_meta, output.landmarks)

        await pipeline.stop()
    """

    def __init__(
        self,
        config: CaptureConfig | None = None,
        output_queue_size: int = 30,
    ) -> None:
        self._config = config or get_config().capture
        self._output_queue_size = output_queue_size

        # Components
        self._webcam = WebcamCapture(self._config)
        self._face_tracker = FaceTracker(self._config)
        self._quality_scorer = FrameQualityScorer(self._config)
        self._frame_skipper = AdaptiveFrameSkipper(self._config.fps)

        # Output queue
        self._output_queue: asyncio.Queue[PipelineOutput] = asyncio.Queue(
            maxsize=output_queue_size
        )

        # Pipeline task
        self._pipeline_task: asyncio.Task | None = None
        self._running = False

        # Metrics
        self._frames_processed = 0
        self._frames_quality_rejected = 0
        self._frames_no_face = 0
        # B3 (Phase 4.1): cumulative count of frames the output queue
        # evicted because a slow consumer let the queue fill. Exposed
        # via ``get_diagnostics`` for the /health endpoint; emits a
        # rate-limited WARNING when more than 10 evictions happen in
        # any rolling 5 s window.
        self._frames_dropped_total: int = 0
        # Sliding-window timestamps (seconds, monotonic) of recent
        # evictions. Trimmed inside ``_record_frame_drop`` to keep its
        # length bounded to entries within the 5 s window.
        self._frame_drop_window_seconds: float = 5.0
        self._frame_drop_warn_threshold: int = 10
        self._frame_drop_timestamps: deque[float] = deque()
        # Monotonic timestamp of the last emitted warning. Used to
        # rate-limit the warning to one per drop-window so a sustained
        # backpressure spike doesn't spam the log.
        self._last_frame_drop_warning_at: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frames_processed(self) -> int:
        return self._frames_processed

    @property
    def frames_quality_rejected(self) -> int:
        return self._frames_quality_rejected

    @property
    def frames_skipped(self) -> int:
        return self._frame_skipper.total_skipped

    @property
    def frames_dropped_total(self) -> int:
        """B3 (Phase 4.1): cumulative count of evicted frames at the
        *output* (pipeline → state-engine) queue.

        A nonzero value means downstream (state engine, broadcast loop)
        could not keep up with capture and the pipeline had to drop
        frames to bound queue memory.

        Phase 4 fix #4: this is the AUTHORITATIVE pipeline-level drop
        counter (exposed by ``/health`` and consumed by the dashboard).
        :attr:`WebcamCapture.frames_dropped` measures a different stage —
        *input*-side drops at the webcam → pipeline hand-off queue.
        Operators reading diagnostics see both via :meth:`get_diagnostics`
        and can distinguish "capture too fast" from "pipeline too slow".
        """
        return self._frames_dropped_total

    @property
    def frames_dropped_input(self) -> int:
        """Phase 4 fix #4: input-side drops, mirrored from the underlying
        :class:`WebcamCapture`. Counts evictions at the webcam-thread →
        asyncio-loop queue (i.e. raw capture faster than the pipeline can
        consume). Distinct from :attr:`frames_dropped_total` which counts
        evictions at the pipeline → consumer queue.
        """
        return int(getattr(self._webcam, "frames_dropped", 0) or 0)

    def get_diagnostics(self) -> dict[str, int | float]:
        """B3 (Phase 4.1): operator-facing diagnostics snapshot.

        Returns a flat dict suitable for embedding in a /health response
        or a structured log line. Keys are documented above on the
        individual counters.

        Phase 4 fix #4: ``frames_dropped_total`` is the pipeline → consumer
        drop counter (slow consumer); ``frames_dropped_input`` is the
        webcam → pipeline drop counter (fast camera, slow pipeline). Both
        are surfaced so operators can pinpoint which stage is congested.
        """
        return {
            "frames_processed": self._frames_processed,
            "frames_quality_rejected": self._frames_quality_rejected,
            "frames_no_face": self._frames_no_face,
            "frames_skipped": self._frame_skipper.total_skipped,
            "frames_dropped_total": self._frames_dropped_total,
            "frames_dropped_input": self.frames_dropped_input,
        }

    def _record_frame_drop(self) -> None:
        """B3 (Phase 4.1): instrument an output-queue eviction.

        Increments the cumulative counter, prunes the sliding window of
        recent drops to the last ``_frame_drop_window_seconds`` worth of
        entries, and emits ONE warning per window if the threshold is
        exceeded.
        """
        self._frames_dropped_total += 1
        now = time.monotonic()
        window = self._frame_drop_window_seconds
        self._frame_drop_timestamps.append(now)
        cutoff = now - window
        # Trim entries that fell out of the rolling window. The deque is
        # ordered by insertion so a single popleft loop is O(n) amortised
        # across all calls — never quadratic.
        while (
            self._frame_drop_timestamps
            and self._frame_drop_timestamps[0] < cutoff
        ):
            self._frame_drop_timestamps.popleft()
        if (
            len(self._frame_drop_timestamps) > self._frame_drop_warn_threshold
            and (now - self._last_frame_drop_warning_at) >= window
        ):
            self._last_frame_drop_warning_at = now
            logger.warning(
                "Capture pipeline backpressure: %d frames dropped in "
                "%.0fs (total=%d). Downstream consumer is slow.",
                len(self._frame_drop_timestamps),
                window,
                self._frames_dropped_total,
            )

    async def start(self) -> None:
        """Start the full capture pipeline."""
        if self._running:
            logger.warning("CapturePipeline already running")
            return

        # Initialize components
        self._face_tracker.initialize()
        await self._webcam.start()

        # Start pipeline processing loop
        self._running = True
        self._pipeline_task = asyncio.create_task(
            self._pipeline_loop(), name="capture-pipeline"
        )
        logger.info("CapturePipeline started")

    async def stop(self) -> None:
        """Stop the capture pipeline and release all resources."""
        self._running = False

        if self._pipeline_task is not None:
            self._pipeline_task.cancel()
            try:
                await self._pipeline_task
            except asyncio.CancelledError:
                pass
            self._pipeline_task = None

        await self._webcam.stop()
        self._face_tracker.release()

        logger.info(
            "CapturePipeline stopped",
            extra={
                "processed": self._frames_processed,
                "quality_rejected": self._frames_quality_rejected,
                "no_face": self._frames_no_face,
                "skipped": self._frame_skipper.total_skipped,
                # B3 (Phase 4.1): include the drop counter in the
                # shutdown diagnostics so the value lands in archived
                # daemon logs without requiring a /health request.
                "dropped": self._frames_dropped_total,
            },
        )

    async def get_output(self, timeout: float = 1.0) -> PipelineOutput | None:
        """
        Get the next pipeline output.

        Args:
            timeout: Maximum wait time in seconds.

        Returns:
            PipelineOutput or None on timeout.
        """
        try:
            return await asyncio.wait_for(self._output_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def __aiter__(self):
        """Async iterate over pipeline outputs."""
        while self._running:
            output = await self.get_output(timeout=0.5)
            if output is not None:
                yield output

    async def _pipeline_loop(self) -> None:
        """Main pipeline processing loop."""
        try:
            while self._running:
                # Get frame from webcam
                captured = await self._webcam.get_frame(timeout=0.5)
                if captured is None:
                    continue

                # Adaptive frame skip
                if self._frame_skipper.should_skip(captured.sequence):
                    continue

                # Process frame
                t_start = time.monotonic()
                output = self._process_frame(captured)
                processing_time = time.monotonic() - t_start

                # Update adaptive skip with processing latency
                self._frame_skipper.update_latency(processing_time)

                if output is None:
                    continue

                # Publish to output queue
                if self._output_queue.full():
                    # Drop oldest to maintain real-time
                    try:
                        self._output_queue.get_nowait()
                        # B3 (Phase 4.1): record the eviction.
                        self._record_frame_drop()
                    except asyncio.QueueEmpty:
                        # Race: another consumer drained the queue between
                        # the ``full()`` check and ``get_nowait`` call.
                        # Benign — fall through to the put_nowait below.
                        logger.debug(
                            "frame drop race: queue drained between full() and get_nowait",
                        )

                try:
                    self._output_queue.put_nowait(output)
                except asyncio.QueueFull:
                    # B3 (Phase 4.1): even after dropping the oldest the
                    # queue is still full (consumer added another
                    # producer concurrently?). Record this as a drop too
                    # so the counter reflects ALL frames lost.
                    self._record_frame_drop()
                    logger.debug(
                        "frame drop: put_nowait racing with refill",
                    )

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error in capture pipeline loop")

    def _process_frame(self, captured: CapturedFrame) -> PipelineOutput | None:
        """
        Process a single captured frame through face tracking and quality scoring.

        Args:
            captured: Raw captured frame.

        Returns:
            PipelineOutput if frame passes quality gate, None otherwise.
        """
        frame = captured.frame

        # audit Phase-I: convert BGR→RGB and BGR→GRAY exactly once per
        # frame and share the cached views between detectors. The
        # FaceTracker takes the RGB view (it would otherwise call
        # cvtColor itself); the QualityScorer takes the GRAY view (it
        # would otherwise convert twice — once per metric). On an
        # M-series Mac at 30 Hz / 640×480 this halves the per-frame
        # cvtColor cost.
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Step 1: Face tracking
        tracking = self._face_tracker.process_frame(frame, rgb_frame=rgb_frame)

        # Step 2: Compute motion displacement
        nose_displacement = 0.0
        if tracking.face_detected and tracking.landmarks_px is not None:
            nose_displacement = self._face_tracker.compute_nose_tip_displacement(
                tracking.landmarks_px
            )

        # Step 3: Quality scoring
        quality = self._quality_scorer.score(
            frame, nose_displacement, gray_frame=gray_frame,
        )

        # Step 4: Build FrameMeta.
        # P1 Pipeline A: stamp ``low_quality`` directly on the meta so
        # downstream consumers do not have to read the full QualityScore
        # struct. The rPPG window in ``runtime_daemon._rgb_history`` now
        # skips the RGB append (or substitutes a NaN sentinel) when this
        # flag is True instead of polluting the bvp signal with frames
        # the quality scorer already rejected.
        frame_meta = FrameMeta(
            timestamp=captured.timestamp,
            face_detected=tracking.face_detected,
            face_confidence=tracking.confidence,
            brightness_score=quality.brightness_score,
            blur_score=quality.blur_score,
            motion_score=quality.motion_score,
            low_quality=not quality.passed,
        )

        self._frames_processed += 1

        # Step 5: Quality gate
        if not quality.passed:
            self._frames_quality_rejected += 1
            logger.debug(
                "Frame at t=%.3f rejected by quality gate "
                "(brightness=%.2f blur=%.2f motion=%.2f); "
                "frame_meta.low_quality=True, downstream must skip RGB sample.",
                captured.timestamp,
                quality.brightness_score,
                quality.blur_score,
                quality.motion_score,
            )

        if not tracking.face_detected and not tracking.face_stable:
            self._frames_no_face += 1

        return PipelineOutput(
            frame_meta=frame_meta,
            landmarks=tracking.landmarks,
            landmarks_px=tracking.landmarks_px,
            frame=frame,
            quality=quality,
            tracking=tracking,
        )
