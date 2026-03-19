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
    "mac studio",
    "mac pro",
    "mac mini",
)

_CONTINUITY_CAMERA_KEYWORDS = (
    "iphone",
    "ipad",
    "continuity",
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
    """Enumerate macOS camera names in AVFoundation order.

    Retries once after a short delay if the initial enumeration returns empty,
    as AVFoundation may not have discovered all devices yet during early startup.
    """
    if not is_macos():
        return []

    names = _list_macos_video_device_names_once()
    if not names:
        # AVFoundation device discovery can be slow at startup — retry after delay
        import time as _time
        _time.sleep(1.0)
        names = _list_macos_video_device_names_once()

    if names:
        logger.info("Enumerated %d camera(s): %s", len(names), names)
    else:
        logger.warning("Could not enumerate macOS cameras via AVFoundation")
    return names


def _list_macos_video_device_names_once() -> list[str]:
    """Single attempt to enumerate macOS camera names."""
    # Try the pyobjc AVFoundation wrapper first
    try:
        import AVFoundation
        devices = (
            AVFoundation.AVCaptureDevice.devicesWithMediaType_(
                AVFoundation.AVMediaTypeVideo
            )
            or []
        )
        return [_extract_objc_string(device, "localizedName") for device in devices]
    except ImportError:
        pass
    except Exception:
        logger.exception("Failed to enumerate macOS cameras via AVFoundation")
        return []

    # Fallback: load AVFoundation via objc bridge (pyobjc-core only)
    try:
        import objc
        objc.loadBundle(
            "AVFoundation",
            bundle_path="/System/Library/Frameworks/AVFoundation.framework",
            module_globals={},
        )
        AVCaptureDevice = objc.lookUpClass("AVCaptureDevice")
        devices = AVCaptureDevice.devicesWithMediaType_("vide") or []
        return [_extract_objc_string(device, "localizedName") for device in devices]
    except Exception:
        logger.debug("Failed to enumerate macOS cameras via objc bridge", exc_info=True)

    return []


def _llm_pick_builtin_camera(names: list[str]) -> int | None:
    """Use a local LLM to identify which camera is the computer's built-in webcam.

    Calls local Ollama with a tiny classification prompt.  Returns the 0-based
    index of the chosen camera, or None if the LLM is unavailable.
    """
    if len(names) < 2:
        return 0 if names else None

    numbered = "\n".join(f"{i}: {n}" for i, n in enumerate(names))
    prompt = (
        "Which of these cameras is the computer's built-in webcam "
        "(not a phone or external camera)? Reply with ONLY the number.\n\n"
        f"{numbered}"
    )

    try:
        import httpx
        resp = httpx.post(
            "http://localhost:11434/api/generate",
            json={"model": "llama3.2", "prompt": prompt, "stream": False},
            timeout=8.0,
        )
        if resp.status_code == 200:
            text = resp.json().get("response", "").strip()
            # Extract first digit from the response
            for ch in text:
                if ch.isdigit():
                    idx = int(ch)
                    if 0 <= idx < len(names):
                        logger.info("LLM selected camera %d (%s) from %d candidates", idx, names[idx], len(names))
                        return idx
    except Exception:
        logger.debug("LLM camera selection unavailable, using keyword fallback")
    return None


def _find_builtin_macos_camera() -> tuple[int | None, str | None]:
    """Return the first built-in Mac camera, skipping Continuity Camera devices.

    Uses a local LLM for classification when available, with keyword-based
    fallback.  AVFoundation may list an iPhone Continuity Camera before the
    built-in camera, or macOS may reorder devices when the iPhone connects.
    """
    names = _list_macos_video_device_names()

    # Try LLM-based selection first
    llm_pick = _llm_pick_builtin_camera(names)
    if llm_pick is not None:
        return llm_pick, names[llm_pick]

    # Keyword fallback — first pass: match built-in keywords, skip Continuity Camera
    for index, name in enumerate(names):
        normalized = name.casefold()
        is_continuity = any(kw in normalized for kw in _CONTINUITY_CAMERA_KEYWORDS)
        if is_continuity:
            continue
        if any(keyword in normalized for keyword in _BUILTIN_MAC_CAMERA_KEYWORDS):
            return index, name
    # Second pass: return the first non-Continuity device (even without keyword match)
    for index, name in enumerate(names):
        normalized = name.casefold()
        if not any(kw in normalized for kw in _CONTINUITY_CAMERA_KEYWORDS):
            return index, name
    return None, None


def _iter_camera_candidates(config: CaptureConfig) -> Iterable[CameraSelection]:
    """
    Yield camera candidates in preference order.

    When no device_id is configured on macOS, we enumerate all cameras and
    yield built-in Mac cameras first, then other non-phone cameras, then
    Continuity Camera (iPhone/iPad) last.  This ensures the Mac's own camera
    is always preferred over Continuity Camera.

    Because AVFoundation's device enumeration index doesn't always match the
    OpenCV device index (especially when Continuity Camera is present), we
    try ALL available device indices — not just the one AVFoundation reports.
    """
    requested_device_id = (
        _AUTO_CAMERA_DEVICE_ID if config.device_id is None else config.device_id
    )

    candidates: list[CameraSelection] = []

    if config.device_id is None and is_macos():
        names = _list_macos_video_device_names()
        if names:
            # Try LLM-based selection first — if it picks a non-Continuity camera, use it
            llm_pick = _llm_pick_builtin_camera(names)
            if llm_pick is not None:
                pick_name = names[llm_pick].casefold()
                if not any(kw in pick_name for kw in _CONTINUITY_CAMERA_KEYWORDS):
                    candidates.append(
                        CameraSelection(
                            device_id=llm_pick,
                            backend=cv2.CAP_AVFOUNDATION,
                            source="llm_selected",
                            device_name=names[llm_pick],
                        )
                    )

            # Partition into builtin, other, and continuity camera groups
            builtin: list[tuple[int, str]] = []
            other: list[tuple[int, str]] = []
            continuity: list[tuple[int, str]] = []

            for idx, name in enumerate(names):
                normalized = name.casefold()
                if any(kw in normalized for kw in _CONTINUITY_CAMERA_KEYWORDS):
                    continuity.append((idx, name))
                elif any(kw in normalized for kw in _BUILTIN_MAC_CAMERA_KEYWORDS):
                    builtin.append((idx, name))
                else:
                    other.append((idx, name))

            # Yield in order: builtin first, then other, then continuity last
            for group, source in [
                (builtin, "builtin_mac_camera"),
                (other, "other_camera"),
                (continuity, "continuity_camera"),
            ]:
                for idx, name in group:
                    candidates.append(
                        CameraSelection(
                            device_id=idx,
                            backend=cv2.CAP_AVFOUNDATION,
                            source=source,
                            device_name=name,
                        )
                    )

            # Also try ALL indices without explicit backend (in same order)
            for group, source in [
                (builtin, "builtin_mac_camera"),
                (other, "other_camera"),
                (continuity, "continuity_camera"),
            ]:
                for idx, name in group:
                    candidates.append(
                        CameraSelection(
                            device_id=idx,
                            backend=None,
                            source=source,
                            device_name=name,
                        )
                    )

    # Fallback: try device indices 0-4 as "probe" candidates.
    # The post-open check in open_video_capture() will re-enumerate and
    # reject any Continuity Camera, so these are safe to try.
    if is_macos():
        for probe_idx in range(5):
            candidates.append(
                CameraSelection(
                    device_id=probe_idx,
                    backend=cv2.CAP_AVFOUNDATION,
                    source="probe_device",
                )
            )
        for probe_idx in range(5):
            candidates.append(
                CameraSelection(
                    device_id=probe_idx,
                    backend=None,
                    source="probe_device",
                )
            )
    else:
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


def _request_macos_camera_permission() -> bool:
    """Explicitly request camera permission from macOS TCC.

    On first launch, this triggers the system "Allow camera?" dialog.
    Returns True if authorized, False if denied or unavailable.
    """
    if not is_macos():
        return True
    try:
        import objc
        import threading

        objc.loadBundle(
            "AVFoundation",
            bundle_path="/System/Library/Frameworks/AVFoundation.framework",
            module_globals={},
        )
        AVCaptureDevice = objc.lookUpClass("AVCaptureDevice")

        # 0=notDetermined, 1=restricted, 2=denied, 3=authorized
        status = AVCaptureDevice.authorizationStatusForMediaType_("vide")
        if status == 3:
            return True
        if status in (1, 2):
            logger.warning("Camera access denied (TCC status=%d)", status)
            return False

        # Status is notDetermined — trigger the system permission dialog
        logger.info("Requesting camera permission from macOS...")
        result_event = threading.Event()
        granted = [False]

        def on_response(was_granted: bool) -> None:
            granted[0] = was_granted
            result_event.set()

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            "vide", on_response,
        )
        # Wait up to 60s for user to click Allow/Deny
        result_event.wait(timeout=60)
        logger.info("Camera permission %s", "granted" if granted[0] else "denied")
        return granted[0]
    except Exception:
        logger.debug("Could not request camera permission via AVFoundation", exc_info=True)
        return True  # Can't check — proceed and let OpenCV try


def open_video_capture(
    config: CaptureConfig,
) -> tuple[cv2.VideoCapture | None, CameraSelection | None]:
    """Open the best matching webcam device for the given configuration.

    Validates each candidate by reading a test frame — some cameras report as
    open but fail to deliver frames (e.g. when permissions are denied or the
    device is in an incompatible mode).
    """
    import time as _time

    # Request camera permission before trying to open any device.
    # On first run this triggers the macOS "Allow camera?" dialog.
    if is_macos():
        _request_macos_camera_permission()

    last_candidate: CameraSelection | None = None

    for candidate in _iter_camera_candidates(config):
        last_candidate = candidate
        logger.info(
            "Trying camera candidate: device_id=%d, source=%s, name=%s, backend=%s",
            candidate.device_id,
            candidate.source,
            candidate.device_name or "unknown",
            candidate.backend,
        )

        # Skip Continuity Camera candidates entirely — never open them
        if candidate.source == "continuity_camera":
            logger.info(
                "Skipping device %d (%s) — Continuity Camera",
                candidate.device_id,
                candidate.device_name or "unknown",
            )
            continue

        capture = (
            cv2.VideoCapture(candidate.device_id, candidate.backend)
            if candidate.backend is not None
            else cv2.VideoCapture(candidate.device_id)
        )
        if capture.isOpened():
            # Validate with test frame reads — built-in Mac cameras can need
            # up to ~1.5s to deliver the first frame after opening.
            ret, frame = False, None
            for _attempt in range(4):
                _time.sleep(0.5)
                ret, frame = capture.read()
                if ret and frame is not None:
                    break
            if ret and frame is not None:
                # ALWAYS re-enumerate to verify the camera at this index.
                # Camera order can change dynamically (iPhone Continuity Camera
                # can appear/disappear between our initial enum and now).
                live_names = _list_macos_video_device_names()
                actual_name = None
                if live_names and candidate.device_id < len(live_names):
                    actual_name = live_names[candidate.device_id]
                # Fall back to cached name only if live enum returned nothing
                if actual_name is None:
                    actual_name = candidate.device_name

                if actual_name and any(
                    kw in actual_name.casefold()
                    for kw in _CONTINUITY_CAMERA_KEYWORDS
                ):
                    logger.info(
                        "Skipping device %d (%s) — Continuity Camera detected post-open",
                        candidate.device_id,
                        actual_name,
                    )
                    capture.release()
                    continue

                logger.info(
                    "Opened camera device %d (%s) — %s",
                    candidate.device_id,
                    actual_name or candidate.source,
                    f"{frame.shape[1]}x{frame.shape[0]}",
                )
                return capture, candidate
            logger.debug(
                "Camera device %d (%s) opened but no frames, skipping",
                candidate.device_id,
                candidate.device_name or candidate.source,
            )
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
        """Stop the webcam capture and release resources.

        Always releases the camera device, even if the capture thread has
        already exited on its own.
        """
        # Signal the capture thread to stop (idempotent)
        self._running.clear()

        # Wait for thread to finish if it's still alive
        if self._thread is not None:
            self._stopped.wait(timeout=2.0)
            self._thread = None

        # ALWAYS release the camera — this is the critical cleanup
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
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
