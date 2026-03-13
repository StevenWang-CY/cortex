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
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

from cortex.libs.config.settings import CaptureConfig
from cortex.libs.utils.platform import is_macos

logger = logging.getLogger(__name__)
_AUTO_CAMERA_DEVICE_ID = 0
_BUILTIN_MAC_CAMERA_KEYWORDS = (
    "facetime",
    "built-in",
    "macbook",
    "imac",
)


@dataclass(frozen=True)
class CapturedFrame:
    """A single captured webcam frame with metadata."""

    frame: np.ndarray  # BGR uint8, shape (H, W, 3)
    timestamp: float  # time.monotonic() seconds
    sequence: int  # monotonically increasing frame counter


@dataclass(frozen=True)
class CameraSelection:
    """Concrete camera selection used to open a VideoCapture device."""

    device_id: int
    backend: int | None
    source: str
    device_name: str | None = None


def describe_requested_camera(config: CaptureConfig) -> str:
    """Human-readable description of the configured camera preference."""
    return "auto" if config.device_id is None else str(config.device_id)


def _extract_objc_string(obj: object, attr: str) -> str:
    """Best-effort conversion of an Objective-C property/method to str."""
    value = getattr(obj, attr, None)
    if value is None:
        return ""
    try:
        value = value() if callable(value) else value
    except TypeError:
        pass
    return str(value or "")


def _list_macos_video_device_names() -> list[str]:
    """Enumerate macOS camera names in AVFoundation order."""
    if not is_macos():
        return []

    try:
        import AVFoundation
    except ImportError:
        return []

    try:
        devices = (
            AVFoundation.AVCaptureDevice.devicesWithMediaType_(
                AVFoundation.AVMediaTypeVideo
            )
            or []
        )
    except Exception:
        logger.exception("Failed to enumerate macOS cameras")
        return []

    return [_extract_objc_string(device, "localizedName") for device in devices]


def _find_builtin_macos_camera() -> tuple[int | None, str | None]:
    """Return the first built-in Mac camera if one is visible to AVFoundation."""
    for index, name in enumerate(_list_macos_video_device_names()):
        normalized = name.casefold()
        if any(keyword in normalized for keyword in _BUILTIN_MAC_CAMERA_KEYWORDS):
            return index, name
    return None, None


def _iter_camera_candidates(config: CaptureConfig) -> Iterable[CameraSelection]:
    """
    Yield camera candidates in preference order.

    If no device is configured, macOS prefers the built-in camera first and then
    falls back to the platform default device ordering.
    """
    requested_device_id = (
        _AUTO_CAMERA_DEVICE_ID if config.device_id is None else config.device_id
    )

    candidates: list[CameraSelection] = []

    if config.device_id is None and is_macos():
        builtin_index, builtin_name = _find_builtin_macos_camera()
        if builtin_index is not None:
            candidates.append(
                CameraSelection(
                    device_id=builtin_index,
                    backend=cv2.CAP_AVFOUNDATION,
                    source="builtin_mac_camera",
                    device_name=builtin_name,
                )
            )
            candidates.append(
                CameraSelection(
                    device_id=builtin_index,
                    backend=None,
                    source="builtin_mac_camera",
                    device_name=builtin_name,
                )
            )

    if is_macos():
        candidates.append(
            CameraSelection(
                device_id=requested_device_id,
                backend=cv2.CAP_AVFOUNDATION,
                source="configured_device",
            )
        )

    candidates.append(
        CameraSelection(
            device_id=requested_device_id,
            backend=None,
            source="configured_device",
        )
    )

    seen: set[tuple[int, int | None]] = set()
    for candidate in candidates:
        key = (candidate.device_id, candidate.backend)
        if key in seen:
            continue
        seen.add(key)
        yield candidate


def open_video_capture(
    config: CaptureConfig,
) -> tuple[cv2.VideoCapture | None, CameraSelection | None]:
    """Open the best matching webcam device for the given configuration."""
    last_candidate: CameraSelection | None = None

    for candidate in _iter_camera_candidates(config):
        last_candidate = candidate
        capture = (
            cv2.VideoCapture(candidate.device_id, candidate.backend)
            if candidate.backend is not None
            else cv2.VideoCapture(candidate.device_id)
        )
        if capture.isOpened():
            return capture, candidate
        capture.release()

    return None, last_candidate


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
        self._camera_selection: CameraSelection | None = None

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
        self._cap, self._camera_selection = open_video_capture(self._config)
        if self._cap is None or not self._cap.isOpened():
            raise RuntimeError(
                f"Cannot open webcam device {describe_requested_camera(self._config)}"
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
                "requested_device_id": describe_requested_camera(self._config),
                "device_id": self._camera_selection.device_id,
                "camera_source": self._camera_selection.source,
                "camera_name": self._camera_selection.device_name,
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
