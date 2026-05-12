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
import sys
from functools import lru_cache

logger = logging.getLogger(__name__)


def _is_macos() -> bool:
    return sys.platform == "darwin"


@lru_cache(maxsize=1)
def _log_unsupported_once(reason: str) -> None:
    logger.info("Receptivity probe unsupported: %s", reason)


def is_microphone_in_use() -> bool | None:
    """Best-effort detection of an active microphone capture session.

    Implementation strategy on macOS:

    * Prefer ``CoreAudio`` ``kAudioHardwarePropertyProcessIsRunning``
      (any process with an input stream open).
    * Fall back to scanning ``AVCaptureDevice``s for an active session.

    Returns ``True`` / ``False`` on success; ``None`` if neither API is
    available (so the caller can degrade gracefully).
    """
    if not _is_macos():
        _log_unsupported_once("not macOS")
        return None
    try:
        import CoreAudio  # type: ignore[import-not-found]
    except ImportError:
        _log_unsupported_once("CoreAudio framework missing")
        return None
    try:
        prop = CoreAudio.AudioObjectPropertyAddress(
            CoreAudio.kAudioHardwarePropertyProcessIsRunning,
            CoreAudio.kAudioObjectPropertyScopeGlobal,
            CoreAudio.kAudioObjectPropertyElementMain,
        )
        _, running = CoreAudio.AudioObjectGetPropertyData(
            CoreAudio.kAudioObjectSystemObject,
            prop,
            0,
            None,
            CoreAudio.UInt32(0),
        )
        return bool(running)
    except Exception:  # noqa: BLE001
        # Property not available on this macOS revision — return None so
        # the daemon doesn't false-positive.
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
        import Quartz  # type: ignore[import-not-found]
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
