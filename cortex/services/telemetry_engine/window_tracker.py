"""
Telemetry Engine — Window Tracker

Tracks active window/app focus changes using platform-specific APIs.
Records window switch events for computing window_switch_rate and
identifying task context.

Platform support:
- macOS: pyobjc (NSWorkspace)
- Linux: python-xlib / ewmh (optional)
- Windows: ctypes (optional)

Falls back gracefully when platform-specific libraries are unavailable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from cortex.libs.utils.platform import Platform, get_platform

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowFocusEvent:
    """A window focus change event."""

    timestamp: float
    app_name: str
    window_title: str
    bundle_id: str | None = None  # macOS only


class WindowTracker:
    """
    Tracks active window focus changes.

    Polls the active window at a configurable interval and records
    focus change events when the active window changes.

    Usage:
        tracker = WindowTracker(poll_interval=1.0)
        tracker.start()
        events = tracker.get_events_in_window(window_seconds=15.0)
        tracker.stop()
    """

    def __init__(
        self,
        poll_interval: float = 1.0,
        history_size: int = 1000,
    ) -> None:
        self._poll_interval = poll_interval
        self._events: deque[WindowFocusEvent] = deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_app_name: str | None = None
        self._last_window_title: str | None = None
        self._platform = get_platform()
        self._available = self._check_availability()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_available(self) -> bool:
        return self._available

    def _check_availability(self) -> bool:
        """Check if window tracking is available on this platform."""
        if self._platform == Platform.MACOS:
            try:
                from AppKit import NSWorkspace  # noqa: F401
                return True
            except ImportError:
                logger.info("pyobjc not available — window tracking disabled")
                return False
        elif self._platform == Platform.LINUX:
            try:
                import Xlib  # noqa: F401
                return True
            except ImportError:
                logger.info("python-xlib not available — window tracking disabled")
                return False
        elif self._platform == Platform.WINDOWS:
            try:
                import ctypes  # noqa: F401
                return True
            except ImportError:
                return False
        return False

    def start(self) -> bool:
        """
        Start window tracking in a background thread.

        Returns:
            True if tracking started, False if unavailable.
        """
        if self._running:
            return True

        if not self._available:
            logger.warning("Window tracking not available on this platform")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="window-tracker"
        )
        self._thread.start()
        logger.info("Window tracker started")
        return True

    def stop(self) -> None:
        """Stop window tracking."""
        if not self._running:
            return

        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Window tracker stopped")

    def record_focus_event(
        self,
        app_name: str,
        window_title: str = "",
        bundle_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """
        Manually record a window focus event (for testing or external sources).

        Only records if the focus actually changed from the previous state.
        """
        t = timestamp if timestamp is not None else time.monotonic()

        if app_name == self._last_app_name and window_title == self._last_window_title:
            return  # No change

        event = WindowFocusEvent(
            timestamp=t,
            app_name=app_name,
            window_title=window_title,
            bundle_id=bundle_id,
        )

        with self._lock:
            self._events.append(event)

        self._last_app_name = app_name
        self._last_window_title = window_title

    def get_events_in_window(
        self,
        window_seconds: float = 15.0,
        current_time: float | None = None,
    ) -> list[WindowFocusEvent]:
        """
        Get window focus events within the specified time window.

        Args:
            window_seconds: Time window to look back.
            current_time: Reference time. Defaults to now.

        Returns:
            List of window focus events in chronological order.
        """
        now = current_time or time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            return [e for e in self._events if e.timestamp >= cutoff]

    def _poll_loop(self) -> None:
        """Background polling loop for active window detection."""
        while self._running:
            try:
                info = self._get_active_window_info()
                if info is not None:
                    app_name, window_title, bundle_id = info
                    self.record_focus_event(app_name, window_title, bundle_id)
            except Exception as e:
                logger.debug(f"Window tracking poll error: {e}")

            time.sleep(self._poll_interval)

    def _get_active_window_info(self) -> tuple[str, str, str | None] | None:
        """
        Get the currently active window information.

        Returns:
            (app_name, window_title, bundle_id) or None if unavailable.
        """
        if self._platform == Platform.MACOS:
            return self._get_active_window_macos()
        elif self._platform == Platform.LINUX:
            return self._get_active_window_linux()
        elif self._platform == Platform.WINDOWS:
            return self._get_active_window_windows()
        return None

    def _get_active_window_macos(self) -> tuple[str, str, str | None] | None:
        """Get active window on macOS using NSWorkspace."""
        try:
            from AppKit import NSWorkspace

            workspace = NSWorkspace.sharedWorkspace()
            active_app = workspace.activeApplication()

            if active_app is None:
                return None

            app_name = active_app.get("NSApplicationName", "Unknown")
            bundle_id = active_app.get("NSApplicationBundleIdentifier")

            # Window title requires accessibility API (Quartz)
            # For now, use app name as window title fallback
            window_title = app_name

            try:
                from Quartz import (
                    CGWindowListCopyWindowInfo,
                    kCGNullWindowID,
                    kCGWindowListOptionOnScreenOnly,
                )

                window_list = CGWindowListCopyWindowInfo(
                    kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                )
                if window_list:
                    pid = active_app.get("NSApplicationProcessIdentifier", 0)
                    for window in window_list:
                        if window.get("kCGWindowOwnerPID") == pid:
                            title = window.get("kCGWindowName", "")
                            if title:
                                window_title = title
                                break
            except ImportError:
                pass

            return (app_name, window_title, bundle_id)

        except Exception as e:
            logger.debug(f"macOS window info error: {e}")
            return None

    def _get_active_window_linux(self) -> tuple[str, str, str | None] | None:
        """Get active window on Linux using python-xlib."""
        try:
            import Xlib
            from Xlib import display as xdisplay

            d = xdisplay.Display()
            root = d.screen().root
            net_active_window = d.intern_atom("_NET_ACTIVE_WINDOW")
            active_window_id = root.get_full_property(
                net_active_window, Xlib.X.AnyPropertyType
            )

            if not active_window_id:
                return None

            window_id = active_window_id.value[0]
            window = d.create_resource_object("window", window_id)

            # Get window name
            net_wm_name = d.intern_atom("_NET_WM_NAME")
            wm_name = window.get_full_property(net_wm_name, Xlib.X.AnyPropertyType)
            window_title = wm_name.value.decode() if wm_name else "Unknown"

            # Get WM_CLASS for app name
            wm_class = window.get_wm_class()
            app_name = wm_class[1] if wm_class else "Unknown"

            d.close()
            return (app_name, window_title, None)

        except Exception as e:
            logger.debug(f"Linux window info error: {e}")
            return None

    def _get_active_window_windows(self) -> tuple[str, str, str | None] | None:
        """Get active window on Windows using ctypes."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()

            if not hwnd:
                return None

            # Get window title
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            window_title = buf.value or "Unknown"

            # Get process name
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            import psutil

            try:
                proc = psutil.Process(pid.value)
                app_name = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                app_name = "Unknown"

            return (app_name, window_title, None)

        except Exception as e:
            logger.debug(f"Windows window info error: {e}")
            return None

    def reset(self) -> None:
        """Clear event history."""
        with self._lock:
            self._events.clear()
        self._last_app_name = None
        self._last_window_title = None
