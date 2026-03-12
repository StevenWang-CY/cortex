"""
Capture Service — Threaded Webcam Capture

Provides a threaded OpenCV VideoCapture that acquires frames at stable FPS,
timestamps each frame with monotonic clock, and publishes to an async queue.

Design:
- Separate capture thread to avoid blocking the async event loop
- Monotonic timestamps for drift-free timing
- Configurable FPS targeting with frame timing correction
- Graceful start/stop with resource cleanup
- No frames are saved to disk (privacy-first)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from cortex.libs.config.settings import CaptureConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedFrame:
    """A single captured webcam frame with metadata."""

    frame: np.ndarray  # BGR uint8, shape (H, W, 3)
    timestamp: float  # time.monotonic() seconds
    sequence: int  # monotonically increasing frame counter


class WebcamCapture:
    """
    Threaded webcam capture with stable FPS targeting.

    Runs a dedicated capture thread that reads frames from OpenCV VideoCapture
    and places them into an asyncio-safe queue for downstream consumption.

    Usage:
        capture = WebcamCapture(config)
        await capture.start()
        frame = await capture.get_frame()
        await capture.stop()
    """

    def __init__(
        self,
        config: CaptureConfig | None = None,
        queue_maxsize: int = 30,
    ) -> None:
        self._config = config or CaptureConfig()
        self._queue_maxsize = queue_maxsize

        # State
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()  # Initially stopped

        # Async queue for cross-thread communication
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[CapturedFrame] | None = None

        # Metrics
        self._sequence = 0
        self._frames_captured = 0
        self._frames_dropped = 0
        self._last_fps_time = 0.0
        self._fps_frame_count = 0
        self._measured_fps = 0.0

    @property
    def is_running(self) -> bool:
        """Check if capture is currently running."""
        return self._running.is_set()

    @property
    def measured_fps(self) -> float:
        """Get the measured FPS over the last reporting interval."""
        return self._measured_fps

    @property
    def frames_captured(self) -> int:
        """Total frames captured since start."""
        return self._frames_captured

    @property
    def frames_dropped(self) -> int:
        """Total frames dropped due to full queue."""
        return self._frames_dropped

    async def start(self) -> None:
        """
        Start the webcam capture thread.

        Raises:
            RuntimeError: If webcam cannot be opened.
        """
        if self._running.is_set():
            logger.warning("WebcamCapture already running")
            return

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)

        # Open camera
        self._cap = cv2.VideoCapture(self._config.device_id)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"Cannot open webcam device {self._config.device_id}"
            )

        # Configure camera
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height)
        self._cap.set(cv2.CAP_PROP_FPS, self._config.fps)

        # Reset counters
        self._sequence = 0
        self._frames_captured = 0
        self._frames_dropped = 0
        self._last_fps_time = time.monotonic()
        self._fps_frame_count = 0

        # Start capture thread
        self._stopped.clear()
        self._running.set()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="cortex-webcam",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "WebcamCapture started",
            extra={
                "device_id": self._config.device_id,
                "resolution": f"{self._config.width}x{self._config.height}",
                "target_fps": self._config.fps,
            },
        )

    async def stop(self) -> None:
        """Stop the webcam capture and release resources."""
        if not self._running.is_set():
            return

        self._running.clear()

        # Wait for thread to finish
        if self._thread is not None:
            # Give it up to 2 seconds to finish
            self._stopped.wait(timeout=2.0)
            self._thread = None

        # Release camera
        if self._cap is not None:
            self._cap.release()
            self._cap = None

        logger.info(
            "WebcamCapture stopped",
            extra={
                "total_captured": self._frames_captured,
                "total_dropped": self._frames_dropped,
            },
        )

    async def get_frame(self, timeout: float = 1.0) -> CapturedFrame | None:
        """
        Get the next captured frame.

        Args:
            timeout: Maximum wait time in seconds.

        Returns:
            CapturedFrame or None if timeout.
        """
        if self._queue is None:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def get_frame_nowait(self) -> CapturedFrame | None:
        """
        Get a frame without waiting.

        Returns:
            CapturedFrame or None if no frame available.
        """
        if self._queue is None:
            return None
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def _capture_loop(self) -> None:
        """Main capture loop running in a dedicated thread."""
        target_interval = 1.0 / self._config.fps
        next_capture_time = time.monotonic()

        try:
            while self._running.is_set():
                now = time.monotonic()

                # FPS timing: wait until next frame is due
                sleep_time = next_capture_time - now
                if sleep_time > 0.001:  # Only sleep if > 1ms
                    time.sleep(sleep_time)

                # Read frame
                if self._cap is None or not self._cap.isOpened():
                    logger.error("Webcam lost")
                    break

                ret, frame = self._cap.read()
                timestamp = time.monotonic()

                if not ret or frame is None:
                    logger.warning("Failed to read frame from webcam")
                    next_capture_time = timestamp + target_interval
                    continue

                # Create captured frame
                captured = CapturedFrame(
                    frame=frame,
                    timestamp=timestamp,
                    sequence=self._sequence,
                )
                self._sequence += 1
                self._frames_captured += 1

                # Publish to async queue (non-blocking)
                self._enqueue_frame(captured)

                # Update FPS measurement
                self._fps_frame_count += 1
                elapsed = timestamp - self._last_fps_time
                if elapsed >= 1.0:
                    self._measured_fps = self._fps_frame_count / elapsed
                    self._fps_frame_count = 0
                    self._last_fps_time = timestamp

                # Schedule next capture
                next_capture_time += target_interval
                # If we've fallen behind, reset to avoid burst capture
                if next_capture_time < timestamp - target_interval:
                    next_capture_time = timestamp + target_interval

        except Exception:
            logger.exception("Error in capture loop")
        finally:
            self._running.clear()
            self._stopped.set()

    def _enqueue_frame(self, frame: CapturedFrame) -> None:
        """Thread-safe enqueue of a frame to the async queue."""
        if self._loop is None or self._queue is None:
            return

        try:
            self._loop.call_soon_threadsafe(self._try_put, frame)
        except RuntimeError:
            # Event loop closed
            pass

    def _try_put(self, frame: CapturedFrame) -> None:
        """Try to put a frame in the queue, dropping oldest if full."""
        if self._queue is None:
            return

        if self._queue.full():
            # Drop oldest frame to maintain real-time
            try:
                self._queue.get_nowait()
                self._frames_dropped += 1
            except asyncio.QueueEmpty:
                pass

        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._frames_dropped += 1
