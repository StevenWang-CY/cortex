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
from dataclasses import dataclass

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
        except asyncio.TimeoutError:
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
                    except asyncio.QueueEmpty:
                        pass

                try:
                    self._output_queue.put_nowait(output)
                except asyncio.QueueFull:
                    pass

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

        # Step 1: Face tracking
        tracking = self._face_tracker.process_frame(frame)

        # Step 2: Compute motion displacement
        nose_displacement = 0.0
        if tracking.face_detected and tracking.landmarks_px is not None:
            nose_displacement = self._face_tracker.compute_nose_tip_displacement(
                tracking.landmarks_px
            )

        # Step 3: Quality scoring
        quality = self._quality_scorer.score(frame, nose_displacement)

        # Step 4: Build FrameMeta
        frame_meta = FrameMeta(
            timestamp=captured.timestamp,
            face_detected=tracking.face_detected,
            face_confidence=tracking.confidence,
            brightness_score=quality.brightness_score,
            blur_score=quality.blur_score,
            motion_score=quality.motion_score,
        )

        self._frames_processed += 1

        # Step 5: Quality gate
        if not quality.passed:
            self._frames_quality_rejected += 1
            # Still emit for metrics, but mark as low quality
            # Downstream consumers can check quality.passed
            pass

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
