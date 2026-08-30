"""
Capture Service — Threaded Webcam Capture

Provides a threaded OpenCV VideoCapture that acquires scheduled observations
at stable FPS, stamps each attempt with dual clocks, and publishes successes
and failures to an async queue.

Design:
- Separate capture thread to avoid blocking the async event loop
- Monotonic timestamps for drift-free timing
- Configurable FPS targeting with frame timing correction
- Graceful start/stop with resource cleanup
- No frames are saved to disk (privacy-first)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID, uuid4

import cv2
import numpy as np
from numpy.typing import NDArray

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds
from cortex.libs.config.settings import CaptureConfig
from cortex.libs.schemas.observations import CameraIdentity, MissingReason
from cortex.libs.schemas.temporal import EventTime
from cortex.libs.utils.platform import (
    CameraPermissionState,
    get_camera_permission_state,
    is_macos,
)

logger = logging.getLogger(__name__)
_AUTO_CAMERA_DEVICE_ID = 0

# Phase 4 fix #3: how many consecutive failed ``cap.read()`` calls before we
# flag capture as stale.  Count alone is not sufficient because AVFoundation
# may block for roughly a second before returning one failed read.  The
# wall-clock bound below prevents a nominal "30 frames" threshold from taking
# 30 seconds on a genuinely stalled device.
_CAPTURE_STALE_THRESHOLD: int = 30
_CAPTURE_STALE_AFTER_SECONDS: float = 2.0

# A camera that validated during ``open_video_capture`` can still be
# interrupted after startup.  OpenCV exposes that interruption only as
# repeated ``read()`` failures, so the capture owner must release and reopen
# the device.  The first retry is quick; subsequent attempts are capped to
# avoid thrashing AVFoundation or repeatedly waking a Continuity Camera.
_CAPTURE_RECOVERY_BACKOFF_SECONDS: tuple[float, ...] = (
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    15.0,
)
_CAPTURE_POST_REOPEN_FAILURE_THRESHOLD: int = 3
_FAILED_READ_LOG_INTERVAL_SECONDS: float = 5.0
_CAPTURE_THREAD_STOP_TIMEOUT_SECONDS: float = 2.0
_CAPTURE_EMERGENCY_RELEASE_GRACE_SECONDS: float = 0.5
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
    """One scheduler-owned camera observation.

    ``frame`` is absent when the scheduled read failed.  Legacy constructors
    may omit the explicit event tuple for one internal migration release;
    production capture always supplies the complete tuple.
    """

    frame: NDArray[np.uint8] | None  # BGR uint8, shape (H, W, 3), or missing
    timestamp: float  # UNIX epoch seconds (time.time()), to match FrameMeta.timestamp schema
    sequence: int  # monotonically increasing frame counter
    observed_at_unix_ms: int | None = None
    observed_at_mono_ns: int | None = None
    boot_id: UUID | None = None
    source_instance_id: UUID | None = None
    camera_identity: CameraIdentity | None = None
    missing_reason: MissingReason | None = None

    def __post_init__(self) -> None:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("captured frame event-time fields must be supplied together")
        if self.frame is None and self.missing_reason is None:
            raise ValueError("a missing frame requires a missing_reason")
        if self.frame is not None and self.missing_reason is not None:
            raise ValueError("a captured frame cannot carry a missing_reason")


@dataclass(frozen=True)
class CameraSelection:
    """Concrete camera selection used to open a VideoCapture device."""

    device_id: int
    backend: int | None
    source: str
    device_name: str | None = None
    device_key: str | None = None
    is_continuity: bool = False

    def identity(self, *, width: int, height: int) -> CameraIdentity:
        """Return an index-reorder-safe camera identity.

        AVFoundation indices are intentionally excluded from the identity
        hash.  On macOS, ``device_key`` is a one-way digest of AVFoundation's
        reboot-stable ``uniqueID`` and therefore survives device-index
        reordering without exposing the platform identifier.  The normalized
        name/source fallback is retained for non-macOS and older bridges that
        cannot expose ``uniqueID``.
        """

        normalized_name = " ".join((self.device_name or "unknown").casefold().split())
        stable_device = self.device_key or f"{self.source}\0{normalized_name}"
        material = f"{stable_device}\0{width}x{height}"
        identity_key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return CameraIdentity(
            identity_key=identity_key,
            device_id=self.device_id,
            device_name=self.device_name,
            source=self.source,
            backend=self.backend,
            width=width,
            height=height,
        )


@dataclass(frozen=True)
class MacCameraDevice:
    """One AVFoundation device in the exact order OpenCV indexes on macOS.

    ``device_key`` is deliberately a digest rather than AVFoundation's raw
    ``uniqueID``.  It is safe to retain in runtime telemetry and gives the
    calibration boundary a physical-device identity that does not change when
    Continuity Camera reshuffles numeric indices.
    """

    index: int
    name: str
    device_key: str | None
    is_continuity: bool
    is_connected: bool | None = None
    device_type: str = ""

    @property
    def is_builtin(self) -> bool:
        normalized_name = self.name.casefold()
        normalized_type = self.device_type.casefold().replace("_", "-")
        return (
            any(keyword in normalized_name for keyword in _BUILTIN_MAC_CAMERA_KEYWORDS)
            or "built-in" in normalized_type
            or "builtin" in normalized_type
        )


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
    except Exception:
        logger.debug("Failed to read AVFoundation property %s", attr, exc_info=True)
        return ""
    return str(value or "")


def _extract_objc_bool(obj: object, attr: str) -> bool | None:
    """Best-effort conversion of an optional Objective-C boolean property."""

    value = getattr(obj, attr, None)
    if value is None:
        return None
    try:
        value = value() if callable(value) else value
    except Exception:
        logger.debug("Failed to read AVFoundation property %s", attr, exc_info=True)
        return None
    return bool(value)


def _camera_device_key(raw_unique_id: str) -> str | None:
    """Return a privacy-preserving stable key for an AVFoundation unique ID."""

    normalized = raw_unique_id.strip()
    if not normalized:
        return None
    material = f"cortex-avfoundation-device\0{normalized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _device_from_avfoundation(index: int, device: object) -> MacCameraDevice:
    """Convert an Objective-C capture device without retaining raw IDs."""

    name = _extract_objc_string(device, "localizedName").strip()
    explicit_continuity = _extract_objc_bool(device, "isContinuityCamera")
    keyword_continuity = any(
        keyword in name.casefold() for keyword in _CONTINUITY_CAMERA_KEYWORDS
    )
    return MacCameraDevice(
        index=index,
        name=name,
        device_key=_camera_device_key(_extract_objc_string(device, "uniqueID")),
        is_continuity=(
            explicit_continuity
            if explicit_continuity is not None
            else keyword_continuity
        ),
        is_connected=_extract_objc_bool(device, "isConnected"),
        device_type=_extract_objc_string(device, "deviceType"),
    )


def _list_macos_video_devices() -> list[MacCameraDevice]:
    """Enumerate verified AVFoundation camera descriptors in OpenCV order.

    OpenCV's AVFoundation backend selects from the legacy
    ``devicesWithMediaType:`` array by numeric index.  Using that same array is
    intentional: a modern discovery-session order is not guaranteed to map to
    OpenCV's index.  We still consume modern per-device properties such as
    ``isContinuityCamera`` and ``uniqueID``.
    """

    if not is_macos():
        return []

    devices = _list_macos_video_devices_once()
    if not devices:
        # Discovery can lag during early app startup.  This is enumeration
        # only; it never opens a capture device or requests TCC authority.
        time.sleep(1.0)
        devices = _list_macos_video_devices_once()

    if devices:
        logger.info(
            "Enumerated %d camera(s): built_in=%d other=%d continuity=%d "
            "disconnected=%d",
            len(devices),
            sum(device.is_builtin for device in devices),
            sum(not device.is_builtin and not device.is_continuity for device in devices),
            sum(device.is_continuity for device in devices),
            sum(device.is_connected is False for device in devices),
        )
    else:
        logger.warning("Could not enumerate macOS cameras via AVFoundation")
    return devices


def _list_macos_video_device_names() -> list[str]:
    """Compatibility view of :func:`_list_macos_video_devices` names."""

    return [device.name for device in _list_macos_video_devices()]


def _list_macos_video_devices_once() -> list[MacCameraDevice]:
    """Single attempt to enumerate macOS camera descriptors."""
    # Try the pyobjc AVFoundation wrapper first
    try:
        import AVFoundation
        devices = (
            AVFoundation.AVCaptureDevice.devicesWithMediaType_(
                AVFoundation.AVMediaTypeVideo
            )
            or []
        )
        return [
            _device_from_avfoundation(index, device)
            for index, device in enumerate(devices)
        ]
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
        return [
            _device_from_avfoundation(index, device)
            for index, device in enumerate(devices)
        ]
    except Exception:
        logger.debug("Failed to enumerate macOS cameras via objc bridge", exc_info=True)

    return []


def _iter_camera_candidates(config: CaptureConfig) -> Iterable[CameraSelection]:
    """
    Yield camera candidates in preference order.

    On macOS the descriptor array is the same AVFoundation array indexed by
    OpenCV's backend.  That array can reorder when a device connects or
    disconnects, so every candidate is still re-enumerated after warm-up.
    Continuity Camera, disconnected, and unnamed devices never become OpenCV
    candidates.  Automatic selection prefers built-in hardware, then other
    verified non-phone cameras.
    """
    requested_device_id = (
        _AUTO_CAMERA_DEVICE_ID if config.device_id is None else config.device_id
    )

    candidates: list[CameraSelection] = []
    macos = is_macos()

    if macos:
        devices = _list_macos_video_devices()
        if not devices:
            # Blind numeric probes can wake an iPhone before the mandatory
            # post-open verification rejects it.  Failing closed avoids that
            # privacy regression and leaves Cortex in telemetry-first mode.
            logger.warning(
                "No verified macOS camera identities are available; refusing "
                "blind device-index probes"
            )
            return

        if config.device_id is not None:
            live_device = next(
                (device for device in devices if device.index == requested_device_id),
                None,
            )
            if live_device is not None:
                if not live_device.name:
                    logger.warning(
                        "Configured camera index %d has no verified device name; "
                        "capture remains offline",
                        requested_device_id,
                    )
                elif live_device.is_continuity:
                    logger.warning(
                        "Configured device %d is Continuity Camera; refusing "
                        "to open it",
                        requested_device_id,
                    )
                elif live_device.is_connected is False:
                    logger.warning(
                        "Configured device %d is disconnected; capture remains "
                        "offline",
                        requested_device_id,
                    )
                else:
                    for backend in (cv2.CAP_AVFOUNDATION, None):
                        candidates.append(
                            CameraSelection(
                                device_id=requested_device_id,
                                backend=backend,
                                source="configured_device",
                                device_name=live_device.name,
                                device_key=live_device.device_key,
                            )
                        )
            else:
                logger.warning(
                    "Configured camera index %d has no live AVFoundation identity; "
                    "capture remains offline",
                    requested_device_id,
                )
        else:
            eligible_devices = [
                device
                for device in devices
                if device.name
                and not device.is_continuity
                and device.is_connected is not False
            ]
            ordered_devices = [
                *(device for device in eligible_devices if device.is_builtin),
                *(device for device in eligible_devices if not device.is_builtin),
            ]
            for backend in (cv2.CAP_AVFOUNDATION, None):
                for device in ordered_devices:
                    candidates.append(
                        CameraSelection(
                            device_id=device.index,
                            backend=backend,
                            source=(
                                "builtin_mac_camera"
                                if device.is_builtin
                                else "other_camera"
                            ),
                            device_name=device.name,
                            device_key=device.device_key,
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


def _macos_camera_permission_is_authorized() -> bool:
    """Return whether this process already has macOS camera authority.

    The runtime deliberately does **not** request TCC authority here.  The
    desktop onboarding surface owns that user gesture through the
    non-blocking helper in :mod:`cortex.libs.utils.platform`.  Blocking the
    daemon thread on ``requestAccess...`` used to hold every transport and
    shutdown path for sixty seconds when the prompt was hidden, ignored, or
    unable to dispatch its callback.

    If AVFoundation cannot be queried we preserve the previous best-effort
    behaviour and let OpenCV perform the final capability check.
    """
    if not is_macos():
        return True
    state = get_camera_permission_state()
    if state == CameraPermissionState.AUTHORIZED:
        return True
    if state == CameraPermissionState.NOT_DETERMINED:
        logger.info(
            "Camera permission has not been requested; capture remains "
            "offline until the user grants it from Cortex onboarding or settings"
        )
        return False
    if state in {
        CameraPermissionState.RESTRICTED,
        CameraPermissionState.DENIED,
    }:
        logger.warning("Camera access unavailable (TCC status=%s)", state.value)
        return False
    if state == CameraPermissionState.UNKNOWN:
        logger.warning("Unknown camera authorization status")
        return False
    logger.debug("Camera authorization query unavailable; trying OpenCV")
    return True


def open_video_capture(
    config: CaptureConfig,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[cv2.VideoCapture | None, CameraSelection | None]:
    """Open the best matching webcam device for the given configuration.

    Validates each candidate by reading a test frame — some cameras report as
    open but fail to deliver frames (e.g. when permissions are denied or the
    device is in an incompatible mode).
    """
    # Permission prompts are an explicit onboarding/settings gesture.  Runtime
    # startup only checks current authority and fails fast into the product's
    # telemetry-first mode.  This keeps UI, HTTP, WebSocket, and quit paths
    # responsive even when TCC is unresolved.
    if is_macos() and not _macos_camera_permission_is_authorized():
        return None, None

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _warmup_pause(seconds: float) -> bool:
        """Wait between warmup reads, returning False when cancelled."""

        if cancel_event is None:
            time.sleep(seconds)
            return True
        return not cancel_event.wait(timeout=seconds)

    last_candidate: CameraSelection | None = None

    for candidate in _iter_camera_candidates(config):
        if _cancelled():
            logger.info("Camera open cancelled before candidate %d", candidate.device_id)
            return None, last_candidate
        last_candidate = candidate
        logger.info(
            "Trying camera candidate: device_id=%d, source=%s, backend=%s",
            candidate.device_id,
            candidate.source,
            candidate.backend,
        )

        # Defense in depth for injected/custom candidate iterators: a known
        # Continuity Camera must never reach OpenCV.
        if candidate.is_continuity or candidate.source == "continuity_camera":
            logger.info(
                "Skipping device %d — Continuity Camera",
                candidate.device_id,
            )
            continue

        capture = (
            cv2.VideoCapture(candidate.device_id, candidate.backend)
            if candidate.backend is not None
            else cv2.VideoCapture(candidate.device_id)
        )
        if _cancelled():
            capture.release()
            return None, last_candidate
        if capture.isOpened():
            # Validate with test frame reads — built-in Mac cameras can need
            # up to ~1.5s to deliver the first frame after opening.
            ret, frame = False, None
            for _attempt in range(4):
                if not _warmup_pause(0.5):
                    capture.release()
                    return None, last_candidate
                ret, frame = capture.read()
                if ret and frame is not None:
                    break
            if ret and frame is not None:
                # ALWAYS re-enumerate to verify the camera at this index.
                # Camera order can change dynamically (iPhone Continuity Camera
                # can appear/disappear between our initial enum and now).
                actual_name: str | None = candidate.device_name
                actual_device: MacCameraDevice | None = None
                if is_macos():
                    live_devices = _list_macos_video_devices()
                    actual_name = None
                    actual_device = next(
                        (
                            device
                            for device in live_devices
                            if device.index == candidate.device_id
                        ),
                        None,
                    )
                    if actual_device is not None:
                        actual_name = actual_device.name or None

                if actual_device is not None and actual_device.is_continuity:
                    logger.info(
                        "Skipping device %d — Continuity Camera detected post-open",
                        candidate.device_id,
                    )
                    capture.release()
                    continue

                if actual_device is not None and actual_device.is_connected is False:
                    logger.info(
                        "Skipping device %d — disconnected during post-open "
                        "verification",
                        candidate.device_id,
                    )
                    capture.release()
                    continue

                # On macOS, reject cameras whose identity we cannot verify.
                # A missing descriptor/name means the index is beyond the live
                # enumeration range or cannot be mapped.  This often indicates
                # a device reshuffle while OpenCV was warming up.
                if is_macos() and (actual_device is None or actual_name is None):
                    logger.info(
                        "Skipping device %d — live post-open camera identity "
                        "could not be verified",
                        candidate.device_id,
                    )
                    capture.release()
                    continue

                live_source = candidate.source
                if actual_device is not None and actual_device.is_builtin:
                    live_source = "builtin_mac_camera"
                elif actual_device is not None:
                    live_source = "other_camera"
                logger.info(
                    "Opened camera device %d (%s) — %s",
                    candidate.device_id,
                    live_source,
                    f"{frame.shape[1]}x{frame.shape[0]}",
                )
                if _cancelled():
                    capture.release()
                    return None, last_candidate
                return capture, replace(
                    candidate,
                    source=live_source,
                    device_name=actual_name,
                    device_key=(
                        actual_device.device_key
                        if actual_device is not None
                        else candidate.device_key
                    ),
                    is_continuity=False,
                )
            logger.debug(
                "Camera device %d (%s) opened but no frames, skipping",
                candidate.device_id,
                candidate.source,
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
        *,
        clock: Clock | None = None,
    ) -> None:
        self._config = config or CaptureConfig()
        self._clock = clock or SYSTEM_CLOCK
        self._queue_maxsize = queue_maxsize

        # State
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()  # Initially stopped
        # Cooperative cancellation for the blocking OpenCV/AVFoundation
        # worker.  All backend operations stay on one serial camera thread;
        # the event loop receives only startup completion.
        self._open_cancel = threading.Event()
        self._startup_future: asyncio.Future[None] | None = None

        # Async queue for cross-thread communication
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[CapturedFrame] | None = None

        # Metrics
        self._sequence = 0
        self._frames_captured = 0
        # Per-arm drop counters (renamed for clarity — see ``frames_dropped``
        # docstring). The pipeline-side ``CapturePipeline.frames_dropped_total``
        # is the authoritative cross-system counter; this one specifically
        # tracks evictions from the *webcam-thread → asyncio-loop* hand-off
        # queue (i.e. "capture too fast for the pipeline to consume").
        self._input_queue_drops = 0
        self._last_fps_time = 0.0
        self._fps_frame_count = 0
        self._measured_fps = 0.0
        self._camera_selection: CameraSelection | None = None
        self._camera_identity: CameraIdentity | None = None
        self._source_instance_id: UUID | None = None
        self._has_successful_frame = False

        # Phase 4 fix #3: consecutive-failure tracking. Incremented on each
        # ``cap.read()`` False/raise, reset on a successful frame. When the
        # count or elapsed-time bound is exceeded, ``_capture_stale`` is set so
        # the daemon's poll path can broadcast a capture-stale signal to the UI.
        self._consecutive_failed_reads: int = 0
        self._capture_stale: bool = False
        self._failed_read_started_mono: float | None = None
        self._last_failed_read_log_mono: float | None = None

        # Recovery is deliberately owned by the same thread that calls
        # ``cap.read()``.  No second worker can read or replace the handle at
        # the same time.  A new source-instance id forces all downstream
        # temporal buffers to reset after a reopen.
        self._recovery_attempts: int = 0
        self._recovery_successes: int = 0
        self._recovery_attempts_since_frame: int = 0
        self._recovery_pending_frame: bool = False

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
        """Total frames dropped at the *input* (webcam-thread → asyncio-loop)
        queue because the pipeline consumer fell behind capture.

        NOTE (Phase 4 fix #4): This counter is distinct from
        :attr:`CapturePipeline.frames_dropped_total`, which counts drops at
        the *output* (pipeline → state-engine) queue. They measure different
        stages — together they tell operators whether a backpressure spike
        is caused by a fast camera, a slow pipeline, or a slow consumer.
        """
        return self._input_queue_drops

    @property
    def capture_stale(self) -> bool:
        """Whether failed reads exceeded the count or elapsed-time bound.

        The runtime daemon polls this in its capture-health watchdog and
        emits a ``capture_stale`` broadcast plus a registry flag when set.
        Recovery keeps it set through reopen and clears it only when the new
        source actually delivers a successful frame.
        """
        return self._capture_stale

    @property
    def recovery_attempts(self) -> int:
        """Number of physical reopen attempts since the latest ``start``."""

        return self._recovery_attempts

    @property
    def recovery_successes(self) -> int:
        """Reopens that subsequently delivered a live frame."""

        return self._recovery_successes

    @property
    def camera_identity(self) -> CameraIdentity | None:
        """Live, post-open camera identity (never a cached pre-open index map)."""

        return self._camera_identity

    @property
    def source_instance_id(self) -> UUID | None:
        """Unique identity for this acquisition/open session."""

        return self._source_instance_id

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
        self._open_cancel.clear()

        # Reset counters
        self._cap = None
        self._camera_selection = None
        self._camera_identity = None
        self._source_instance_id = None
        self._sequence = 0
        self._frames_captured = 0
        self._input_queue_drops = 0
        self._consecutive_failed_reads = 0
        self._capture_stale = False
        self._failed_read_started_mono = None
        self._last_failed_read_log_mono = None
        self._recovery_attempts = 0
        self._recovery_successes = 0
        self._recovery_attempts_since_frame = 0
        self._recovery_pending_frame = False
        self._has_successful_frame = False
        self._last_fps_time = monotonic_seconds(self._clock)
        self._fps_frame_count = 0

        # Enumeration, warmup, configuration, reads, recovery, and release all
        # belong to one serial worker. Passing an opened AVFoundation handle
        # from an ``asyncio.to_thread`` worker into a second thread created an
        # intermittent lifecycle hazard after app relaunch.
        self._stopped.clear()
        self._running.set()
        self._startup_future = self._loop.create_future()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="cortex-webcam",
            daemon=True,
        )
        self._thread.start()
        try:
            await self._startup_future
        except asyncio.CancelledError:
            self._open_cancel.set()
            self._running.clear()
            raise

    def _signal_startup(self, error: Exception | None) -> None:
        """Resolve the event-loop startup future from the camera thread."""

        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._resolve_startup, error)
        except RuntimeError:
            # Event loop closed during process teardown.
            return

    def _resolve_startup(self, error: Exception | None) -> None:
        """Event-loop side of :meth:`_signal_startup`."""

        future = self._startup_future
        if future is None or future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    async def stop(self) -> None:
        """Stop the webcam capture and release resources.

        Always releases the camera device, even if the capture thread has
        already exited on its own.
        """
        # Signal the capture thread to stop (idempotent)
        self._open_cancel.set()
        self._running.clear()

        # Wait off-loop so a blocking backend read cannot freeze the Qt/API
        # event loop during Quit.  The camera thread normally observes the
        # cancellation event immediately.  If a backend call ignores it, an
        # emergency release is the bounded last resort that unblocks the read.
        thread = self._thread
        if thread is not None:
            stopped = await asyncio.to_thread(
                self._stopped.wait,
                _CAPTURE_THREAD_STOP_TIMEOUT_SECONDS,
            )
            if not stopped:
                logger.warning(
                    "Camera thread did not stop within %.1fs; forcing handle release",
                    _CAPTURE_THREAD_STOP_TIMEOUT_SECONDS,
                )
                self._release_active_capture()
                await asyncio.to_thread(
                    self._stopped.wait,
                    _CAPTURE_EMERGENCY_RELEASE_GRACE_SECONDS,
                )
            if not thread.is_alive():
                self._thread = None
            else:
                logger.warning("Camera thread remains alive after emergency release")

        # ALWAYS release the camera — this is the critical cleanup
        # (CLAUDE.md rule 15). If release() raises, that is serious enough to
        # log at WARNING with traceback — a leaked handle blocks future
        # opens until the OS reaps the process.
        self._release_active_capture()

        logger.info(
            "WebcamCapture stopped",
            extra={
                "total_captured": self._frames_captured,
                "total_dropped": self._input_queue_drops,
                "recovery_attempts": self._recovery_attempts,
                "recovery_successes": self._recovery_successes,
            },
        )

    def _configure_capture(self, capture: cv2.VideoCapture) -> None:
        """Apply the requested format to an opened capture handle.

        Backends are allowed to reject individual properties.  That is not a
        reason to discard an otherwise working camera: downstream processing
        already consumes the actual frame dimensions.  Exceptions are logged
        once per property and recovery continues with the backend default.
        """

        properties = (
            (cv2.CAP_PROP_FRAME_WIDTH, self._config.width, "width"),
            (cv2.CAP_PROP_FRAME_HEIGHT, self._config.height, "height"),
            (cv2.CAP_PROP_FPS, self._config.fps, "fps"),
        )
        for property_id, value, name in properties:
            try:
                accepted = capture.set(property_id, value)
            except Exception:
                logger.warning(
                    "Camera backend raised while setting %s=%s; using backend default",
                    name,
                    value,
                    exc_info=True,
                )
                continue
            if accepted is False:
                logger.debug(
                    "Camera backend rejected %s=%s; using backend default",
                    name,
                    value,
                )

    def _release_active_capture(self) -> None:
        """Release and clear the active handle exactly once."""

        capture = self._cap
        self._cap = None
        if capture is None:
            return
        self._safe_release_capture(capture, context="active camera handle")

    @staticmethod
    def _safe_release_capture(
        capture: cv2.VideoCapture,
        *,
        context: str,
    ) -> None:
        """Best-effort release for active, rejected, or cancelled handles."""

        try:
            capture.release()
        except Exception:
            logger.warning(
                "cap.release() raised for %s; camera handle may leak",
                context,
                exc_info=True,
            )

    def _attempt_camera_recovery(self) -> bool:
        """Release, re-enumerate, and reopen a stalled camera.

        Returns ``True`` when a new handle has been installed.  The stale flag
        intentionally remains set until that handle delivers a frame; an open
        handle is not evidence that pixels are flowing.  Cancellation uses the
        same event as startup so shutdown interrupts both backoff and warmup.
        """

        self._capture_stale = True
        self._release_active_capture()

        backoff_index = min(
            self._recovery_attempts_since_frame,
            len(_CAPTURE_RECOVERY_BACKOFF_SECONDS) - 1,
        )
        delay = _CAPTURE_RECOVERY_BACKOFF_SECONDS[backoff_index]
        next_attempt = self._recovery_attempts + 1
        logger.warning(
            "Camera recovery scheduled: attempt=%d backoff_seconds=%.1f "
            "failed_reads=%d",
            next_attempt,
            delay,
            self._consecutive_failed_reads,
        )
        if self._open_cancel.wait(timeout=delay) or not self._running.is_set():
            logger.info("Camera recovery cancelled before reopen")
            return False

        self._recovery_attempts += 1
        self._recovery_attempts_since_frame += 1
        try:
            capture, selection = open_video_capture(
                self._config,
                cancel_event=self._open_cancel,
            )
        except Exception:
            logger.warning(
                "Camera recovery attempt %d raised during reopen",
                self._recovery_attempts,
                exc_info=True,
            )
            return False

        if not self._running.is_set() or self._open_cancel.is_set():
            if capture is not None:
                self._safe_release_capture(
                    capture,
                    context="recovered handle during shutdown",
                )
            return False

        if capture is None or selection is None or not capture.isOpened():
            if capture is not None:
                self._safe_release_capture(
                    capture,
                    context="rejected recovery handle",
                )
            logger.warning(
                "Camera recovery attempt %d could not open a verified device",
                self._recovery_attempts,
            )
            return False

        self._configure_capture(capture)
        self._cap = capture
        self._camera_selection = selection
        self._camera_identity = selection.identity(
            width=self._config.width,
            height=self._config.height,
        )
        self._source_instance_id = uuid4()
        self._has_successful_frame = False
        self._consecutive_failed_reads = 0
        self._failed_read_started_mono = None
        self._last_failed_read_log_mono = None
        self._recovery_pending_frame = True
        logger.info(
            "Camera recovery reopened verified device: attempt=%d device_id=%d "
            "source=%s; awaiting live frame",
            self._recovery_attempts,
            selection.device_id,
            selection.source,
        )
        return True

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
        except TimeoutError:
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
        """Own the complete camera lifecycle on one dedicated thread."""
        target_interval = 1.0 / self._config.fps
        next_capture_time = monotonic_seconds(self._clock)
        startup_succeeded = False

        try:
            capture, selection = open_video_capture(
                self._config,
                cancel_event=self._open_cancel,
            )
            if self._open_cancel.is_set() or not self._running.is_set():
                if capture is not None:
                    self._safe_release_capture(
                        capture,
                        context="cancelled startup handle",
                    )
                raise RuntimeError("Camera startup cancelled")
            if capture is None or selection is None or not capture.isOpened():
                if capture is not None:
                    self._safe_release_capture(
                        capture,
                        context="rejected startup handle",
                    )
                raise RuntimeError(
                    "Cannot open webcam device "
                    f"{describe_requested_camera(self._config)}"
                )

            self._cap = capture
            self._camera_selection = selection
            self._configure_capture(capture)
            self._camera_identity = selection.identity(
                width=self._config.width,
                height=self._config.height,
            )
            self._source_instance_id = uuid4()
            startup_succeeded = True
            logger.info(
                "WebcamCapture started",
                extra={
                    "requested_device_id": describe_requested_camera(self._config),
                    "device_id": selection.device_id,
                    "camera_source": selection.source,
                    "camera_identity_key": self._camera_identity.identity_key,
                    "resolution": f"{self._config.width}x{self._config.height}",
                    "target_fps": self._config.fps,
                },
            )
            self._signal_startup(None)

            while self._running.is_set():
                now = monotonic_seconds(self._clock)

                # FPS timing: wait until next frame is due
                sleep_time = next_capture_time - now
                if sleep_time > 0.001:  # Only sleep if > 1ms
                    if self._open_cancel.wait(timeout=sleep_time):
                        break

                # Read frame
                if self._cap is None or not self._cap.isOpened():
                    if not self._capture_stale:
                        self._capture_stale = True
                        logger.warning(
                            "Camera handle is unavailable; entering recovery"
                        )
                    self._enqueue_frame(
                        self._missing_capture(MissingReason.SOURCE_DISCONNECTED)
                    )
                    self._attempt_camera_recovery()
                    next_capture_time = monotonic_seconds(self._clock) + target_interval
                    continue

                # Phase 4 fix #1: ``CapturedFrame.timestamp`` MUST be UNIX
                # epoch seconds (``time.time()``) to match the
                # ``FrameMeta.timestamp`` schema contract — see
                # cortex/libs/schemas/features.py docstring. Internal timing
                # (FPS, next-capture scheduling) keeps using
                # ``time.monotonic()`` because that clock is drift-free.
                read_failed = False
                read_started_mono = monotonic_seconds(self._clock)
                try:
                    ret, frame = self._cap.read()
                except Exception:
                    logger.warning(
                        "cap.read() raised; treating as failed read",
                        exc_info=True,
                    )
                    ret, frame = False, None
                    read_failed = True
                event_time = EventTime.from_clock(self._clock)
                wall_ts = event_time.observed_at_unix_ms / 1000.0
                mono_ts = event_time.observed_at_mono_ns / 1_000_000_000.0
                sequence = self._sequence
                self._sequence += 1

                if not ret or frame is None:
                    # Phase 4 fix #3: track consecutive read failures so the
                    # daemon's capture-health watchdog can surface a stale
                    # camera before the UI notices on its own.
                    self._consecutive_failed_reads += 1
                    if self._failed_read_started_mono is None:
                        self._failed_read_started_mono = read_started_mono
                    failed_for = max(0.0, mono_ts - self._failed_read_started_mono)
                    recovery_due = (
                        self._consecutive_failed_reads >= _CAPTURE_STALE_THRESHOLD
                        or failed_for >= _CAPTURE_STALE_AFTER_SECONDS
                        or (
                            self._recovery_pending_frame
                            and self._consecutive_failed_reads
                            >= _CAPTURE_POST_REOPEN_FAILURE_THRESHOLD
                        )
                    )
                    if recovery_due and not self._capture_stale:
                        self._capture_stale = True
                        logger.warning(
                            "Capture stalled: failed_reads=%d failed_for_seconds=%.2f "
                            "count_threshold=%d time_threshold_seconds=%.2f; "
                            "flagging capture_stale=True",
                            self._consecutive_failed_reads,
                            failed_for,
                            _CAPTURE_STALE_THRESHOLD,
                            _CAPTURE_STALE_AFTER_SECONDS,
                        )
                    if not read_failed and (
                        self._last_failed_read_log_mono is None
                        or mono_ts - self._last_failed_read_log_mono
                        >= _FAILED_READ_LOG_INTERVAL_SECONDS
                    ):
                        logger.warning(
                            "Failed to read frame from webcam: consecutive=%d "
                            "failed_for_seconds=%.2f",
                            self._consecutive_failed_reads,
                            failed_for,
                        )
                        self._last_failed_read_log_mono = mono_ts
                    reason = (
                        MissingReason.CAMERA_WARMUP
                        if not self._has_successful_frame
                        else MissingReason.SOURCE_DISCONNECTED
                    )
                    self._enqueue_frame(
                        CapturedFrame(
                            frame=None,
                            timestamp=wall_ts,
                            sequence=sequence,
                            observed_at_unix_ms=event_time.observed_at_unix_ms,
                            observed_at_mono_ns=event_time.observed_at_mono_ns,
                            boot_id=event_time.boot_id,
                            source_instance_id=self._source_instance_id,
                            camera_identity=self._camera_identity,
                            missing_reason=reason,
                        )
                    )
                    if recovery_due:
                        self._attempt_camera_recovery()
                    next_capture_time = mono_ts + target_interval
                    continue

                # Successful frame — clear the stale flag if it was set.
                if self._consecutive_failed_reads > 0:
                    if self._capture_stale:
                        logger.info(
                            "Capture recovered after %d failed reads",
                            self._consecutive_failed_reads,
                        )
                    self._consecutive_failed_reads = 0
                    self._capture_stale = False
                    self._failed_read_started_mono = None
                    self._last_failed_read_log_mono = None

                if self._recovery_pending_frame:
                    self._recovery_pending_frame = False
                    self._recovery_successes += 1
                    self._capture_stale = False
                    self._failed_read_started_mono = None
                    self._last_failed_read_log_mono = None
                    logger.info(
                        "Camera recovery completed: attempts=%d successes=%d",
                        self._recovery_attempts,
                        self._recovery_successes,
                    )
                self._recovery_attempts_since_frame = 0

                # Create captured frame (wall-clock timestamp per schema).
                captured = CapturedFrame(
                    frame=cast(NDArray[np.uint8], frame),
                    timestamp=wall_ts,
                    sequence=sequence,
                    observed_at_unix_ms=event_time.observed_at_unix_ms,
                    observed_at_mono_ns=event_time.observed_at_mono_ns,
                    boot_id=event_time.boot_id,
                    source_instance_id=self._source_instance_id,
                    camera_identity=self._camera_identity,
                )
                self._frames_captured += 1
                self._has_successful_frame = True

                # Publish to async queue (non-blocking)
                self._enqueue_frame(captured)

                # Update FPS measurement (uses monotonic clock — drift-free).
                self._fps_frame_count += 1
                elapsed = mono_ts - self._last_fps_time
                if elapsed >= 1.0:
                    self._measured_fps = self._fps_frame_count / elapsed
                    self._fps_frame_count = 0
                    self._last_fps_time = mono_ts

                # Schedule next capture
                next_capture_time += target_interval
                # If we've fallen behind, reset to avoid burst capture
                if next_capture_time < mono_ts - target_interval:
                    next_capture_time = mono_ts + target_interval

        except Exception as exc:
            if startup_succeeded:
                logger.exception("Error in capture loop")
            else:
                self._signal_startup(exc)
        finally:
            self._running.clear()
            self._release_active_capture()
            self._stopped.set()

    def _missing_capture(self, reason: MissingReason) -> CapturedFrame:
        """Create and sequence one missing observation at the scheduler boundary."""

        event_time = EventTime.from_clock(self._clock)
        sequence = self._sequence
        self._sequence += 1
        return CapturedFrame(
            frame=None,
            timestamp=event_time.observed_at_unix_ms / 1000.0,
            sequence=sequence,
            observed_at_unix_ms=event_time.observed_at_unix_ms,
            observed_at_mono_ns=event_time.observed_at_mono_ns,
            boot_id=event_time.boot_id,
            source_instance_id=self._source_instance_id,
            camera_identity=self._camera_identity,
            missing_reason=reason,
        )

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
        """Try to put a frame in the queue, dropping oldest if full.

        Phase 4 fix #4: drops here are *input-side* drops — capture is
        running faster than the asyncio pipeline consumer can drain. They
        are tracked separately from ``CapturePipeline.frames_dropped_total``
        (which measures output-side drops). See the ``frames_dropped``
        property docstring for the rationale.
        """
        if self._queue is None:
            return

        if self._queue.full():
            # Drop oldest frame to maintain real-time
            try:
                self._queue.get_nowait()
                self._input_queue_drops += 1
            except asyncio.QueueEmpty:
                pass

        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._input_queue_drops += 1
