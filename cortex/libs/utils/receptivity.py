"""OS-level receptivity probes (macOS).

Two functions answer questions the trigger policy uses to decide whether
this is a good moment to interrupt the user:

* ``is_microphone_in_use()`` — True if any app is recording (call, mic),
  in which case we don't pop a visual overlay.
* ``is_app_fullscreen()`` — True if the frontmost app's main window is
  on the active display and matches the display's bounds (presentation,
  Zoom share, full-screen Xcode preview…).

Both return ``None`` when the OS doesn't expose the relevant API
(non-macOS or pyobjc absent) so callers can distinguish "no signal" from
"definitely off". The trigger policy treats ``None`` as ``False`` by
default and logs once.
"""

from __future__ import annotations

import logging
import struct
import sys
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


def _is_macos() -> bool:
    return sys.platform == "darwin"


@lru_cache(maxsize=1)
def _log_unsupported_once(reason: str) -> None:
    logger.info("Receptivity probe unsupported: %s", reason)


def _read_uint32_property(
    core_audio: Any,
    null_qualifier: Any,
    audio_object: int,
    selector: int,
) -> int | None:
    """Read one global ``UInt32`` property, or ``None`` when it fails.

    pyobjc maps ``AudioObjectGetPropertyData(object, address, qualifierSize,
    qualifier, ioDataSize, outData)`` to a call returning ``(status, size,
    bytes)``; the qualifier must be ``objc.NULL`` (``None`` fails argument
    conversion).
    """

    address = core_audio.AudioObjectPropertyAddress(
        selector,
        core_audio.kAudioObjectPropertyScopeGlobal,
        getattr(core_audio, "kAudioObjectPropertyElementMain", 0),
    )
    status, size, data = core_audio.AudioObjectGetPropertyData(
        audio_object, address, 0, null_qualifier, 4, None,
    )
    if status != 0 or data is None or int(size) < 4:
        return None
    return int(struct.unpack("=I", bytes(data)[:4])[0])


def is_microphone_in_use() -> bool | None:
    """Best-effort detection of an active microphone capture session.

    Reads ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on the default
    input device: true while any process does I/O on it (a call, a
    recording). The previous probe read
    ``kAudioHardwarePropertyProcessIsRunning``, which the pyobjc binding does
    not export (and which describes only the calling process); the lookup
    raised on every call, so this always answered ``None`` and the
    mic-active receptivity gate never engaged.

    Returns ``True`` / ``False`` on success; ``None`` if the API is not
    available (so the caller can degrade gracefully).
    """
    if not _is_macos():
        _log_unsupported_once("not macOS")
        return None
    try:
        import CoreAudio
        import objc
    except ImportError:
        _log_unsupported_once("CoreAudio framework missing")
        return None
    try:
        default_input = _read_uint32_property(
            CoreAudio,
            objc.NULL,
            CoreAudio.kAudioObjectSystemObject,
            CoreAudio.kAudioHardwarePropertyDefaultInputDevice,
        )
        if default_input is None:
            return None
        if default_input == 0:
            # ``kAudioObjectUnknown``: no input device, so nothing records.
            return False
        running = _read_uint32_property(
            CoreAudio,
            objc.NULL,
            default_input,
            CoreAudio.kAudioDevicePropertyDeviceIsRunningSomewhere,
        )
        return None if running is None else bool(running)
    except Exception:  # noqa: BLE001
        # Property not available on this macOS revision — return None so
        # the daemon doesn't false-positive.
        logger.debug("microphone receptivity probe failed", exc_info=True)
        return None


def is_app_fullscreen() -> bool | None:
    """True when the frontmost window covers its display.

    Uses ``Quartz.CGWindowListCopyWindowInfo`` (no special entitlement
    required) to look at the topmost on-screen window. If its bounds
    match the active display bounds we treat it as full-screen.
    """
    if not _is_macos():
        _log_unsupported_once("not macOS")
        return None
    try:
        import Quartz
    except ImportError:
        _log_unsupported_once("Quartz framework missing")
        return None

    try:
        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )
        windows = Quartz.CGWindowListCopyWindowInfo(
            options, Quartz.kCGNullWindowID,
        )
        if not windows:
            return False

        screens = Quartz.NSScreen.screens() if hasattr(Quartz, "NSScreen") else []
        screen_frames: list[tuple[float, float, float, float]] = []
        for screen in screens or []:
            frame = screen.frame()
            screen_frames.append(
                (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height),
            )

        for window in windows[:8]:  # top few; one of them is the frontmost
            if window.get(Quartz.kCGWindowLayer, 0) != 0:
                continue  # menu bar / dock etc.
            bounds = window.get(Quartz.kCGWindowBounds, {})
            try:
                bw = float(bounds.get("Width", 0.0))
                bh = float(bounds.get("Height", 0.0))
                bx = float(bounds.get("X", 0.0))
                by = float(bounds.get("Y", 0.0))
            except (TypeError, ValueError):
                continue
            for (sx, sy, sw, sh) in screen_frames:
                if abs(bw - sw) < 4.0 and abs(bh - sh) < 4.0 and abs(bx - sx) < 4.0 and abs(by - sy) < 4.0:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return None
