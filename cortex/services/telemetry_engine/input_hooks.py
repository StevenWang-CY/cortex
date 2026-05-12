"""
Telemetry Engine — Input Hooks

Mouse and keyboard event recording using pynput. Records raw events
(position, clicks, scroll, keystrokes) into thread-safe buffers for
downstream feature aggregation.

Design:
- pynput listeners run in background threads
- Events are timestamped with monotonic clock
- Mouse events sampled at 60Hz, downsampled to 10Hz by aggregator
- Keyboard events record inter-keystroke intervals and backspace tracking
- Graceful degradation on PermissionError (macOS accessibility)
- No raw key content is stored — only timing and modifier metadata
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from cortex.libs.config.settings import TelemetryConfig

logger = logging.getLogger(__name__)


class MouseButton(StrEnum):
    """Mouse button types."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"
    UNKNOWN = "unknown"


class ScrollDirection(StrEnum):
    """Scroll direction."""

    UP = "up"
    DOWN = "down"


class KeyType(StrEnum):
    """Key event classification."""

    REGULAR = "regular"
    BACKSPACE = "backspace"
    MODIFIER = "modifier"
    NAVIGATION = "navigation"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MouseMoveEvent:
    """A mouse move event."""

    timestamp: float
    x: int
    y: int


@dataclass(frozen=True)
class MouseClickEvent:
    """A mouse click event."""

    timestamp: float
    x: int
    y: int
    button: MouseButton
    pressed: bool  # True = press, False = release


@dataclass(frozen=True)
class MouseScrollEvent:
    """A mouse scroll event."""

    timestamp: float
    x: int
    y: int
    dx: int
    dy: int
    direction: ScrollDirection


@dataclass(frozen=True)
class KeyEvent:
    """A keyboard event (timing only, no content)."""

    timestamp: float
    key_type: KeyType
    pressed: bool  # True = press, False = release


@dataclass
class InputEventBuffers:
    """Thread-safe event buffers for raw input events."""

    mouse_moves: deque[MouseMoveEvent] = field(
        default_factory=lambda: deque(maxlen=6000)  # 60Hz * 100s
    )
    mouse_clicks: deque[MouseClickEvent] = field(
        default_factory=lambda: deque(maxlen=1000)
    )
    mouse_scrolls: deque[MouseScrollEvent] = field(
        default_factory=lambda: deque(maxlen=1000)
    )
    key_events: deque[KeyEvent] = field(
        default_factory=lambda: deque(maxlen=5000)
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def clear(self) -> None:
        """Clear all buffers."""
        with self.lock:
            self.mouse_moves.clear()
            self.mouse_clicks.clear()
            self.mouse_scrolls.clear()
            self.key_events.clear()


class InputHooks:
    """
    Records mouse and keyboard events using pynput.

    Runs pynput listeners in background threads. Events are stored in
    thread-safe deques for consumption by the feature aggregator.

    Handles macOS accessibility permission gracefully — if denied, the
    listeners simply won't start, and the buffers will remain empty.

    Usage:
        hooks = InputHooks()
        hooks.start()
        # ... events accumulate ...
        events = hooks.get_events_in_window(window_seconds=15.0)
        hooks.stop()
    """

    def __init__(self, config: TelemetryConfig | None = None) -> None:
        self._config = config or TelemetryConfig()
        self._buffers = InputEventBuffers()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._running = False
        self._last_move_time = 0.0
        self._move_interval = 1.0 / self._config.mouse_sample_hz

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def buffers(self) -> InputEventBuffers:
        return self._buffers

    def start(self) -> bool:
        """
        Start input listeners.

        Returns:
            True if listeners started successfully, False on permission error.
        """
        if self._running:
            return True

        try:
            from pynput import keyboard, mouse

            self._mouse_listener = mouse.Listener(
                on_move=self._on_mouse_move,
                on_click=self._on_mouse_click,
                on_scroll=self._on_mouse_scroll,
            )
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )

            self._mouse_listener.start()
            self._keyboard_listener.start()
            self._running = True
            logger.info("Input hooks started (mouse + keyboard listeners)")
            return True

        except ImportError:
            logger.warning("pynput not installed — input hooks disabled")
            return False
        except Exception as e:
            logger.warning(f"Failed to start input hooks: {e}")
            return False

    def stop(self) -> None:
        """Stop input listeners."""
        if not self._running:
            return

        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None

        if self._keyboard_listener is not None:
            self._keyboard_listener.stop()
            self._keyboard_listener = None

        self._running = False
        logger.info("Input hooks stopped")

    def record_mouse_move(self, x: int, y: int, timestamp: float | None = None) -> None:
        """Manually record a mouse move event (for testing or external sources)."""
        t = timestamp if timestamp is not None else time.monotonic()
        event = MouseMoveEvent(timestamp=t, x=x, y=y)
        with self._buffers.lock:
            self._buffers.mouse_moves.append(event)

    def record_mouse_click(
        self, x: int, y: int, button: MouseButton = MouseButton.LEFT,
        pressed: bool = True, timestamp: float | None = None,
    ) -> None:
        """Manually record a mouse click event."""
        t = timestamp if timestamp is not None else time.monotonic()
        event = MouseClickEvent(timestamp=t, x=x, y=y, button=button, pressed=pressed)
        with self._buffers.lock:
            self._buffers.mouse_clicks.append(event)

    def record_mouse_scroll(
        self, x: int, y: int, dx: int, dy: int,
        timestamp: float | None = None,
    ) -> None:
        """Manually record a mouse scroll event."""
        t = timestamp if timestamp is not None else time.monotonic()
        direction = ScrollDirection.UP if dy > 0 else ScrollDirection.DOWN
        event = MouseScrollEvent(
            timestamp=t, x=x, y=y, dx=dx, dy=dy, direction=direction,
        )
        with self._buffers.lock:
            self._buffers.mouse_scrolls.append(event)

    def record_key_event(
        self, key_type: KeyType = KeyType.REGULAR,
        pressed: bool = True, timestamp: float | None = None,
    ) -> None:
        """Manually record a key event."""
        t = timestamp if timestamp is not None else time.monotonic()
        event = KeyEvent(timestamp=t, key_type=key_type, pressed=pressed)
        with self._buffers.lock:
            self._buffers.key_events.append(event)

    def get_events_in_window(
        self, window_seconds: float | None = None, current_time: float | None = None,
    ) -> dict[str, list]:
        """
        Get all events within the specified time window.

        Args:
            window_seconds: Time window to look back. Defaults to config value.
            current_time: Reference time. Defaults to now.

        Returns:
            Dict with lists of events for each type.
        """
        window = window_seconds or self._config.window_seconds
        now = current_time or time.monotonic()
        cutoff = now - window

        with self._buffers.lock:
            moves = [e for e in self._buffers.mouse_moves if e.timestamp >= cutoff]
            clicks = [e for e in self._buffers.mouse_clicks if e.timestamp >= cutoff]
            scrolls = [e for e in self._buffers.mouse_scrolls if e.timestamp >= cutoff]
            keys = [e for e in self._buffers.key_events if e.timestamp >= cutoff]

        return {
            "mouse_moves": moves,
            "mouse_clicks": clicks,
            "mouse_scrolls": scrolls,
            "key_events": keys,
        }

    def _on_mouse_move(self, x: int, y: int) -> None:
        """pynput mouse move callback (rate-limited)."""
        now = time.monotonic()
        # Rate limit to sample_hz
        if now - self._last_move_time < self._move_interval:
            return
        self._last_move_time = now
        event = MouseMoveEvent(timestamp=now, x=x, y=y)
        with self._buffers.lock:
            self._buffers.mouse_moves.append(event)

    def _on_mouse_click(self, x: int, y: int, button: object, pressed: bool) -> None:
        """pynput mouse click callback."""
        now = time.monotonic()
        # Map pynput button to our enum
        btn = MouseButton.UNKNOWN
        btn_name = getattr(button, "name", "")
        if btn_name == "left":
            btn = MouseButton.LEFT
        elif btn_name == "right":
            btn = MouseButton.RIGHT
        elif btn_name == "middle":
            btn = MouseButton.MIDDLE

        event = MouseClickEvent(
            timestamp=now, x=x, y=y, button=btn, pressed=pressed,
        )
        with self._buffers.lock:
            self._buffers.mouse_clicks.append(event)

    def _on_mouse_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """pynput mouse scroll callback."""
        now = time.monotonic()
        direction = ScrollDirection.UP if dy > 0 else ScrollDirection.DOWN
        event = MouseScrollEvent(
            timestamp=now, x=x, y=y, dx=dx, dy=dy, direction=direction,
        )
        with self._buffers.lock:
            self._buffers.mouse_scrolls.append(event)

    def _on_key_press(self, key: object) -> None:
        """pynput key press callback."""
        now = time.monotonic()
        key_type = self._classify_key(key)
        event = KeyEvent(timestamp=now, key_type=key_type, pressed=True)
        with self._buffers.lock:
            self._buffers.key_events.append(event)

    def _on_key_release(self, key: object) -> None:
        """pynput key release callback."""
        now = time.monotonic()
        key_type = self._classify_key(key)
        event = KeyEvent(timestamp=now, key_type=key_type, pressed=False)
        with self._buffers.lock:
            self._buffers.key_events.append(event)

    @staticmethod
    def _classify_key(key: object) -> KeyType:
        """
        Classify a pynput key object into a KeyType.

        Does not store the actual key value for privacy.
        """
        key_name = getattr(key, "name", None) or str(key)

        if key_name == "backspace":
            return KeyType.BACKSPACE
        elif key_name in (
            "shift", "shift_r", "ctrl", "ctrl_r",
            "alt", "alt_r", "cmd", "cmd_r", "caps_lock",
        ):
            return KeyType.MODIFIER
        elif key_name in (
            "up", "down", "left", "right",
            "home", "end", "page_up", "page_down",
            "tab", "enter", "escape",
        ):
            return KeyType.NAVIGATION

        return KeyType.REGULAR

    def reset(self) -> None:
        """Clear all event buffers."""
        self._buffers.clear()
