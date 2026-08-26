"""
Capture Service — Integrated Capture Pipeline

Wires together WebcamCapture → FaceTracker → FrameQualityScorer and publishes
quality-gated FrameMeta + landmarks to an async output queue.

Features:
- Full webcam → face → quality pipeline
- Adaptive frame skip when processing falls behind
- Scheduled observation output (valid, missing, rejected, and stale)
- Structured output for downstream consumers (physio, kinematics)
- Graceful lifecycle management

Privacy: No frames are ever saved to disk. All processing is ephemeral.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

import cv2
import numpy as np
from numpy.typing import NDArray

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds
from cortex.libs.config.settings import CaptureConfig, get_config
from cortex.libs.schemas.features import FrameMeta
from cortex.libs.schemas.observations import (
    CameraFrameObservation,
    CameraIdentity,
    CameraObservationEnvelope,
    MissingReason,
    ObservationSource,
    ObservationValidity,
)
from cortex.services.capture_service.face_tracker import FaceTracker, FaceTrackingResult
from cortex.services.capture_service.observation_buffer import ObservationBuffer
from cortex.services.capture_service.quality import FrameQuality, FrameQualityScorer
from cortex.services.capture_service.webcam import CapturedFrame, WebcamCapture

logger = logging.getLogger(__name__)

_CAPTURE_ALGORITHM_VERSION = "capture-integrity/2.0.0"
_T = TypeVar("_T")


@dataclass(frozen=True)
class PipelineOutput:
    """Output of the capture pipeline for downstream consumers."""

    frame_meta: FrameMeta
    landmarks: NDArray[np.float32] | None  # (468, 3) normalized, or None
    landmarks_px: NDArray[np.float32] | None  # (468, 2) pixel coords, or None
    frame: NDArray[np.uint8] | None  # BGR image (kept in memory only, never saved)
    quality: FrameQuality
    tracking: FaceTrackingResult
    observation: CameraObservationEnvelope
    camera_identity: CameraIdentity


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
        *,
        clock: Clock | None = None,
    ) -> None:
        self._config = config or get_config().capture
        self._clock = clock or SYSTEM_CLOCK
        self._output_queue_size = output_queue_size

        # Components
        self._webcam = WebcamCapture(self._config, clock=self._clock)
        self._face_tracker = FaceTracker(self._config)
        self._quality_scorer = FrameQualityScorer(self._config)
        self._frame_skipper = AdaptiveFrameSkipper(self._config.fps)

        # Output queue
        self._output_queue: asyncio.Queue[PipelineOutput] = asyncio.Queue(
            maxsize=output_queue_size
        )

        # Pipeline task
        self._pipeline_task: asyncio.Task[None] | None = None
        self._running = False
        self._start_lock = asyncio.Lock()
        self._startup_cancel = threading.Event()
        # MediaPipe 0.10.21 (the Intel macOS compatibility wheel) exposes a
        # synchronous TaskRunner without the thread-safe serial dispatcher in
        # newer wheels.  Keep create/detect/close on one owned worker for both
        # architectures.  This also prevents native inference and font/model
        # I/O from blocking the daemon's API/quit event loop.
        self._face_executor: ThreadPoolExecutor | None = None

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
        self._observation_buffer: ObservationBuffer[CameraObservationEnvelope] = ObservationBuffer(
            max_age_seconds=self._config.observation_buffer_seconds,
            max_items=self._config.observation_buffer_max_items,
        )
        self._legacy_source_instance_id = uuid4()
        self._last_captured: CapturedFrame | None = None
        self._observations_missing = 0
        self._observations_rejected = 0

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

    @property
    def camera_recovery_attempts(self) -> int:
        """Physical camera reopen attempts made by the acquisition owner."""

        return int(getattr(self._webcam, "recovery_attempts", 0) or 0)

    @property
    def camera_recovery_successes(self) -> int:
        """Reopens confirmed by a subsequently delivered live frame."""

        return int(getattr(self._webcam, "recovery_successes", 0) or 0)

    @property
    def capture_stale(self) -> bool:
        """Live acquisition-stall state from the camera resource owner."""

        return bool(getattr(self._webcam, "capture_stale", False))

    @property
    def observations(self) -> tuple[CameraObservationEnvelope, ...]:
        """Bounded acquisition-order observation snapshot for diagnostics/replay."""

        return self._observation_buffer.snapshot()

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
            "camera_recovery_attempts": self.camera_recovery_attempts,
            "camera_recovery_successes": self.camera_recovery_successes,
            "observations_buffered": len(self._observation_buffer),
            "observations_missing": self._observations_missing,
            "observations_rejected": self._observations_rejected,
        }

    def _record_frame_drop(self) -> None:
        """B3 (Phase 4.1): instrument an output-queue eviction.

        Increments the cumulative counter, prunes the sliding window of
        recent drops to the last ``_frame_drop_window_seconds`` worth of
        entries, and emits ONE warning per window if the threshold is
        exceeded.
        """
        self._frames_dropped_total += 1
        now = monotonic_seconds(self._clock)
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
        async with self._start_lock:
            if self._running:
                logger.warning("CapturePipeline already running")
                return

            self._startup_cancel.clear()
            # Open the camera first.  A missing TCC grant now fails in
            # milliseconds, avoiding an expensive MediaPipe/font-cache load
            # for a channel that cannot produce frames.
            await self._webcam.start()
            try:
                self._ensure_face_executor()
                await self._run_face_worker(self._initialize_face_tracker)
            except BaseException:
                self._startup_cancel.set()
                await self._webcam.stop()
                await self._release_face_tracker()
                raise

            if self._startup_cancel.is_set():
                await self._webcam.stop()
                await self._release_face_tracker()
                raise asyncio.CancelledError

            self._last_captured = None

            # Start pipeline processing loop
            self._running = True
            self._pipeline_task = asyncio.create_task(
                self._pipeline_loop(), name="capture-pipeline"
            )
            logger.info("CapturePipeline started")

    def _initialize_face_tracker(self) -> None:
        """Initialize MediaPipe on its owned worker."""

        self._face_tracker.initialize()

    def _ensure_face_executor(self) -> ThreadPoolExecutor:
        """Return the single worker that owns every MediaPipe operation."""

        executor = self._face_executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cortex-face-tracker",
            )
            self._face_executor = executor
        return executor

    async def _run_face_worker(
        self,
        fn: Callable[..., _T],
        /,
        *args: Any,
    ) -> _T:
        """Run one synchronous capture operation on the MediaPipe owner."""

        executor = self._face_executor
        if executor is None:
            raise RuntimeError("MediaPipe worker is not available")
        return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)

    async def _release_face_tracker(self) -> None:
        """Close MediaPipe on its owner thread, then retire that worker."""

        executor = self._face_executor
        if executor is None:
            # Under the owned-worker contract, no executor means this pipeline
            # has never initialized MediaPipe or has already released it.
            return
        try:
            await self._run_face_worker(self._face_tracker.release)
        finally:
            self._face_executor = None
            # The release operation above drains all prior work because this
            # is a single-worker executor.  No loop-blocking join is needed.
            executor.shutdown(wait=False, cancel_futures=True)

    async def stop(self) -> None:
        """Stop the capture pipeline and release all resources."""
        # Signal a camera-open/MediaPipe-start task before waiting for the
        # lifecycle lock. The in-flight start then performs its own owner-
        # thread cleanup and releases the lock; teardown below cannot race it
        # into a second close on the event-loop thread.
        self._startup_cancel.set()
        async with self._start_lock:
            self._running = False

            if self._pipeline_task is not None:
                self._pipeline_task.cancel()
                try:
                    await self._pipeline_task
                except asyncio.CancelledError:
                    pass
                self._pipeline_task = None

            await self._webcam.stop()
            await self._release_face_tracker()

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

    async def __aiter__(self) -> AsyncIterator[PipelineOutput]:
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

                for missing in self._synthesize_input_queue_gaps(captured):
                    output = await self._run_face_worker(self._process_frame, missing)
                    await self._publish_output(output)

                # Adaptive frame skip
                if self._frame_skipper.should_skip(captured.sequence):
                    output = await self._run_face_worker(
                        partial(
                            self._missing_output,
                            captured,
                            validity=ObservationValidity.REJECTED,
                            reason=MissingReason.FRAME_DROPPED,
                        )
                    )
                    await self._publish_output(
                        output
                    )
                    continue

                # Process frame
                t_start = self._clock.monotonic_ns()
                output = await self._run_face_worker(self._process_frame, captured)
                processing_time = max(
                    0.0,
                    (self._clock.monotonic_ns() - t_start) / 1_000_000_000.0,
                )

                # Update adaptive skip with processing latency
                self._frame_skipper.update_latency(processing_time)

                await self._publish_output(output)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error in capture pipeline loop")

    def _process_frame(self, captured: CapturedFrame) -> PipelineOutput:
        """
        Process a single captured frame through face tracking and quality scoring.

        Args:
            captured: Raw captured frame.

        Returns:
            PipelineOutput if frame passes quality gate, None otherwise.
        """
        if captured.frame is None:
            return self._missing_output(
                captured,
                validity=ObservationValidity.MISSING,
                reason=captured.missing_reason or MissingReason.UNKNOWN,
            )
        frame = captured.frame
        event = self._event_fields(captured)
        camera_identity = self._camera_identity(captured)

        # audit Phase-I: convert BGR→RGB and BGR→GRAY exactly once per
        # frame and share the cached views between detectors. The
        # FaceTracker takes the RGB view (it would otherwise call
        # cvtColor itself); the QualityScorer takes the GRAY view (it
        # would otherwise convert twice — once per metric). On an
        # M-series Mac at 30 Hz / 640×480 this halves the per-frame
        # cvtColor cost.
        rgb_frame = cast(
            NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        )
        gray_frame = cast(
            NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        )

        # Step 1: Face tracking
        tracking = self._face_tracker.process_frame(
            frame,
            rgb_frame=rgb_frame,
            capture_mono_ns=event[1],
        )

        # Step 3: Quality scoring
        quality = self._quality_scorer.score(
            frame,
            tracking.nose_displacement_px,
            gray_frame=gray_frame,
            motion_face_widths_per_second=tracking.motion_face_widths_per_second,
        )

        # Step 4: Build FrameMeta.
        # Keep the deprecated low_quality mirror while the canonical
        # ObservationEnvelope carries validity, reason, and components.
        frame_meta = FrameMeta(
            timestamp=captured.timestamp,
            observed_at_unix_ms=event[0],
            observed_at_mono_ns=event[1],
            boot_id=event[2],
            frame_available=True,
            missing_reason=None,
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

        validity = ObservationValidity.VALID
        reason: MissingReason | None = None
        if tracking.is_replayed:
            validity = ObservationValidity.STALE
            reason = MissingReason.FRAME_DROPPED
        elif tracking.detector_timestamp_adjusted:
            validity = ObservationValidity.REJECTED
            reason = MissingReason.ARTIFACT
        elif not tracking.face_detected or tracking.landmarks_px is None:
            validity = ObservationValidity.MISSING
            reason = MissingReason.NO_FACE
        elif not quality.passed:
            validity = ObservationValidity.REJECTED
            reason = self._quality_scorer.rejection_reason(quality)

        components = self._quality_components(quality, tracking)
        observation_quality = min(components.values()) if components else 0.0
        value = (
            CameraFrameObservation(
                width=int(frame.shape[1]),
                height=int(frame.shape[0]),
                face_detected=tracking.face_detected,
                face_stable=tracking.face_stable,
                face_confidence=tracking.confidence,
                detector_replayed=tracking.is_replayed,
                detector_timestamp_adjusted=tracking.detector_timestamp_adjusted,
                motion_face_widths_per_second=(
                    tracking.motion_face_widths_per_second
                ),
                camera_identity=camera_identity,
            )
            if validity == ObservationValidity.VALID
            else None
        )
        observation = CameraObservationEnvelope(
            source=ObservationSource.CAMERA,
            source_instance_id=self._source_instance_id(captured),
            sequence=captured.sequence,
            observed_at_unix_ms=event[0],
            observed_at_mono_ns=event[1],
            boot_id=event[2],
            value=value,
            validity=validity,
            missing_reason=reason,
            quality=observation_quality,
            quality_components=components,
            algorithm_version=_CAPTURE_ALGORITHM_VERSION,
        )

        return PipelineOutput(
            frame_meta=frame_meta,
            landmarks=tracking.landmarks,
            landmarks_px=tracking.landmarks_px,
            frame=frame,
            quality=quality,
            tracking=tracking,
            observation=observation,
            camera_identity=camera_identity,
        )

    async def _publish_output(self, output: PipelineOutput) -> None:
        """Retain every observation, then publish with bounded backpressure."""

        self._observation_buffer.append(output.observation)
        if output.observation.validity == ObservationValidity.MISSING.value:
            self._observations_missing += 1
        elif output.observation.validity != ObservationValidity.VALID.value:
            self._observations_rejected += 1

        if self._output_queue.full():
            try:
                self._output_queue.get_nowait()
                self._record_frame_drop()
            except asyncio.QueueEmpty:
                logger.debug(
                    "frame drop race: queue drained between full() and get_nowait",
                )
        try:
            self._output_queue.put_nowait(output)
        except asyncio.QueueFull:
            self._record_frame_drop()
            logger.debug("frame drop: put_nowait racing with refill")

    def _missing_output(
        self,
        captured: CapturedFrame,
        *,
        validity: ObservationValidity,
        reason: MissingReason,
    ) -> PipelineOutput:
        event = self._event_fields(captured)
        camera_identity = self._camera_identity(captured)
        quality = FrameQuality(
            brightness_score=0.0,
            blur_score=0.0,
            motion_score=0.0,
            passed=False,
        )
        if reason in {
            MissingReason.CAMERA_WARMUP,
            MissingReason.SOURCE_DISCONNECTED,
        }:
            tracking = self._face_tracker.process_missing(capture_mono_ns=event[1])
        else:
            tracking = FaceTrackingResult(
                face_detected=False,
                confidence=0.0,
                landmarks=None,
                landmarks_px=None,
                bounding_box=None,
                face_stable=self._face_tracker.face_stable,
                observed_at_mono_ns=event[1],
            )
        frame_meta = FrameMeta(
            timestamp=event[0] / 1000.0,
            observed_at_unix_ms=event[0],
            observed_at_mono_ns=event[1],
            boot_id=event[2],
            frame_available=False,
            missing_reason=reason,
            face_detected=False,
            face_confidence=0.0,
            brightness_score=0.0,
            blur_score=0.0,
            motion_score=0.0,
            low_quality=True,
        )
        observation = CameraObservationEnvelope(
            source=ObservationSource.CAMERA,
            source_instance_id=self._source_instance_id(captured),
            sequence=captured.sequence,
            observed_at_unix_ms=event[0],
            observed_at_mono_ns=event[1],
            boot_id=event[2],
            value=None,
            validity=validity,
            missing_reason=reason,
            quality=0.0,
            quality_components={
                "brightness": 0.0,
                "blur": 0.0,
                "motion": 0.0,
                "face_confidence": 0.0,
            },
            algorithm_version=_CAPTURE_ALGORITHM_VERSION,
        )
        return PipelineOutput(
            frame_meta=frame_meta,
            landmarks=None,
            landmarks_px=None,
            frame=captured.frame,
            quality=quality,
            tracking=tracking,
            observation=observation,
            camera_identity=camera_identity,
        )

    def _event_fields(self, captured: CapturedFrame) -> tuple[int, int, UUID]:
        if (
            captured.observed_at_unix_ms is not None
            and captured.observed_at_mono_ns is not None
            and captured.boot_id is not None
        ):
            return (
                captured.observed_at_unix_ms,
                captured.observed_at_mono_ns,
                captured.boot_id,
            )
        # Internal compatibility for legacy tests/callers. Do not invent a
        # monotonic value from the epoch timestamp; capture it independently.
        return (
            max(0, int(captured.timestamp * 1000)),
            self._clock.monotonic_ns(),
            self._clock.boot_id,
        )

    def _source_instance_id(self, captured: CapturedFrame) -> UUID:
        return captured.source_instance_id or self._legacy_source_instance_id

    def _camera_identity(self, captured: CapturedFrame) -> CameraIdentity:
        if captured.camera_identity is not None:
            return captured.camera_identity
        return CameraIdentity(
            identity_key="legacy-camera",
            device_id=max(0, int(self._config.device_id or 0)),
            device_name=None,
            source="legacy",
            backend=None,
            width=self._config.width,
            height=self._config.height,
        )

    @staticmethod
    def _quality_components(
        quality: FrameQuality,
        tracking: FaceTrackingResult,
    ) -> dict[str, float]:
        return {
            "brightness": float(np.clip(quality.brightness_score, 0.0, 1.0)),
            "blur": float(np.clip(quality.blur_score, 0.0, 1.0)),
            "motion": float(np.clip(quality.motion_score, 0.0, 1.0)),
            "face_confidence": float(np.clip(tracking.confidence, 0.0, 1.0)),
        }

    def _synthesize_input_queue_gaps(
        self, captured: CapturedFrame
    ) -> tuple[CapturedFrame, ...]:
        """Materialize webcam-queue evictions as FRAME_DROPPED observations."""

        previous = self._last_captured
        self._last_captured = captured
        if previous is None:
            return ()
        same_source = self._source_instance_id(previous) == self._source_instance_id(captured)
        gap = captured.sequence - previous.sequence - 1
        if not same_source or gap <= 0:
            return ()
        prev_event = self._event_fields(previous)
        current_event = self._event_fields(captured)
        if current_event[2] != prev_event[2]:
            return ()
        # A pathological consumer stall can span more observations than the
        # bounded buffer can retain. Materialize only the newest bounded tail;
        # the sequence gap still exposes the complete loss count.
        first_offset = max(1, gap - self._config.observation_buffer_max_items + 1)
        missing: list[CapturedFrame] = []
        denominator = gap + 1
        for offset in range(first_offset, gap + 1):
            fraction = offset / denominator
            unix_ms = round(prev_event[0] + fraction * (current_event[0] - prev_event[0]))
            mono_ns = round(prev_event[1] + fraction * (current_event[1] - prev_event[1]))
            missing.append(
                CapturedFrame(
                    frame=None,
                    timestamp=unix_ms / 1000.0,
                    sequence=previous.sequence + offset,
                    observed_at_unix_ms=unix_ms,
                    observed_at_mono_ns=mono_ns,
                    boot_id=current_event[2],
                    source_instance_id=self._source_instance_id(captured),
                    camera_identity=captured.camera_identity,
                    missing_reason=MissingReason.FRAME_DROPPED,
                )
            )
        return tuple(missing)
