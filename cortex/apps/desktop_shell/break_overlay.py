"""
Desktop Shell — Full-screen Biology Break Overlay (P0 §3.7).

A frameless ``QWidget`` that dims the entire screen and paces the user
through a guided breathing pattern (``box`` / ``4-7-8`` / ``coherent``).
The overlay is shown by :class:`BiologyBreakController` via the
``ui_handler`` registered on the daemon.

Design notes:

* Painted directly with ``QPainter`` — no QML, no SVG, no per-frame
  widget reparenting. The breathing circle is one polyline plus one
  filled disc whose radius is animated by a ``QTimer`` at 30 Hz.
* Three audio loops bundled under ``cortex/assets/audio/`` are played
  via :class:`QSoundEffect` (lighter than ``QMediaPlayer`` and avoids
  the FFmpeg dependency in PyInstaller bundles). Audio defaults on but
  is gated by the controller's ``audio_cue`` flag.
* The overlay grabs the keyboard so Escape ends early; an "End early"
  button materialises after ``_END_EARLY_REVEAL_SECONDS`` (60 s) so
  the user can't accidentally bail out in the first minute.
* The ``run`` method is a blocking call (``QEventLoop.exec()``) — the
  desktop controller invokes it on the Qt thread via a queued
  ``QMetaObject.invokeMethod`` call from the asyncio bridge.

Privacy: nothing in this widget touches the network. The audio files
are bundled assets; the breathing animation never reads or writes
anything off-device.
"""

from __future__ import annotations

import logging
import math
import sys
import time
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QEventLoop, QPointF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import QPushButton, QWidget

logger = logging.getLogger(__name__)

# Reveal the "End early" affordance only after this many seconds so the
# user does not bail before the breathing pattern has any chance of
# moving the needle. Matches the P0 design spec.
_END_EARLY_REVEAL_SECONDS = 60

# Breathing pattern cycles. Each value is a list of ``(phase_label,
# seconds)`` tuples; the canvas drives the circle's radius from these
# without hardcoded pattern math.
_PATTERN_CYCLES: dict[str, list[tuple[str, float]]] = {
    "box": [("Inhale", 4.0), ("Hold", 4.0), ("Exhale", 4.0), ("Hold", 4.0)],
    "4-7-8": [("Inhale", 4.0), ("Hold", 7.0), ("Exhale", 8.0)],
    "coherent": [("Inhale", 5.5), ("Exhale", 5.5)],
}

# Audio bundles. Resolved relative to the package root so PyInstaller
# can collect them via ``collect_data_files('cortex.assets.audio')``.
_AUDIO_FILE_BY_PATTERN: dict[str, str] = {
    "box": "box_4s.wav",
    "4-7-8": "box_7_8.wav",
    "coherent": "coherent_5_5.wav",
}


def _audio_root() -> Path:
    """Resolve the bundled audio directory.

    PyInstaller bundles the audio under ``MEIPASS/cortex/assets/audio``
    (see ``cortex/scripts/cortex.spec`` datas tuple). The frozen-build
    branch uses ``sys._MEIPASS`` so the .app can locate the WAV files
    inside ``Contents/Resources/cortex/assets/audio/``; the development
    branch walks two parents up from this file.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "cortex" / "assets" / "audio"
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().parent.parent.parent / "assets" / "audio"


class _BreathingCanvas(QWidget):
    """Inner widget that paints the breathing animation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radius_ratio: float = 0.40
        self._phase_label: str = "Inhale"
        self._setup_palette()

    def _setup_palette(self) -> None:
        self.setAutoFillBackground(False)

    def set_state(self, radius_ratio: float, phase_label: str) -> None:
        self._radius_ratio = max(0.18, min(0.62, float(radius_ratio)))
        self._phase_label = str(phase_label)
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            cx = rect.center().x()
            cy = rect.center().y()
            r = float(min(rect.width(), rect.height())) * self._radius_ratio

            # Background gradient — soft black at the edges so the circle
            # halo blends into the dim overlay.
            grad = QLinearGradient(0, 0, 0, rect.height())
            grad.setColorAt(0.0, QColor(8, 10, 14))
            grad.setColorAt(1.0, QColor(4, 5, 7))
            painter.fillRect(rect, grad)

            # Outer halo (soft ring).
            painter.setBrush(QColor(217, 119, 87, 24))  # warm accent, very low alpha
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(cx, cy), r * 1.45, r * 1.45)

            # Main breathing disc.
            painter.setBrush(QColor(245, 235, 220, 32))  # warm-cream low alpha
            ring_pen = QPen(QColor(217, 119, 87, 200))
            ring_pen.setWidth(2)
            painter.setPen(ring_pen)
            painter.drawEllipse(QPointF(cx, cy), r, r)

            # Phase label in the center.
            painter.setPen(QColor(232, 222, 207))
            phase_font = QFont("Inter", 28)
            phase_font.setWeight(QFont.Weight.Medium)
            painter.setFont(phase_font)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, self._phase_label,
            )
        finally:
            painter.end()


class BreakOverlayWindow(QWidget):
    """Full-screen, frameless break overlay.

    The window blocks input outside the inner canvas. Call :meth:`run`
    with the requested duration / pattern / audio toggle — it returns
    ``(elapsed_seconds, completed)`` once the user either exits early
    or the pattern reaches the requested duration.
    """

    finished = Signal(float, bool)  # elapsed_seconds, completed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CortexBreakOverlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("background-color: #0a0a0c;")

        self._canvas = _BreathingCanvas(self)
        self._end_early_btn: QPushButton | None = None
        self._timer: QTimer | None = None
        self._sound: QSoundEffect | None = None
        self._cycle: list[tuple[str, float]] = _PATTERN_CYCLES["box"]
        self._cycle_total: float = sum(seconds for _, seconds in self._cycle) or 1.0
        self._duration: float = 240.0
        self._start_monotonic: float = 0.0
        self._completed: bool = False
        self._loop: QEventLoop | None = None
        # Avoid double-finished emission when both the timer's
        # duration-reached path and the user's Escape race.
        self._already_finished: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(
        self,
        duration_seconds: float,
        pattern: Literal["box", "4-7-8", "coherent"],
        audio_cue: bool,
    ) -> tuple[float, bool]:
        """Block until the user exits or the pattern completes."""
        self._duration = max(30.0, float(duration_seconds))
        cycle = _PATTERN_CYCLES.get(pattern)
        if cycle is None:
            cycle = _PATTERN_CYCLES["box"]
            pattern = "box"
        self._cycle = cycle
        self._cycle_total = sum(seconds for _, seconds in cycle) or 1.0
        self._completed = False
        self._already_finished = False

        # Load audio (if enabled) — best-effort; if the file is missing
        # we silently proceed without sound rather than aborting.
        self._sound = None
        if audio_cue:
            audio_file = _audio_root() / _AUDIO_FILE_BY_PATTERN[pattern]
            if audio_file.is_file():
                eff = QSoundEffect()
                eff.setSource(QUrl.fromLocalFile(str(audio_file)))
                eff.setVolume(0.35)
                eff.setLoopCount(QSoundEffect.Loop.Infinite)
                eff.play()
                self._sound = eff
            else:
                logger.info(
                    "break audio file missing: %s — proceeding without audio",
                    audio_file,
                )

        # Build the End early button. Hidden until 60 s elapse so the
        # user doesn't bail in the first minute.
        if self._end_early_btn is None:
            btn = QPushButton("End early", self)
            btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255, 255, 255, 0.06);"
                "  color: rgba(232, 222, 207, 0.92);"
                "  border: 1px solid rgba(217, 119, 87, 0.45);"
                "  border-radius: 10px;"
                "  padding: 8px 18px;"
                "  font-family: Inter, system-ui, sans-serif;"
                "  font-size: 12px;"
                "}"
                "QPushButton:hover { background: rgba(255, 255, 255, 0.10); }"
            )
            btn.clicked.connect(self._end_early)
            btn.hide()
            self._end_early_btn = btn

        # Lay out — canvas spans the whole window, button anchored bottom.
        self._layout_children()

        # Fullscreen. Grab keyboard so Escape exits even without focus.
        self.showFullScreen()
        self.activateWindow()
        self.raise_()
        self.grabKeyboard()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

        self._start_monotonic = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 Hz animation
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # Block on a local event loop so the caller can await ``run``
        # synchronously from the Qt thread (matches Qt modal-dialog
        # idiom). The loop exits in ``_finish``.
        self._loop = QEventLoop(self)
        self._loop.exec()
        elapsed = max(0.0, time.monotonic() - self._start_monotonic)
        return elapsed, self._completed

    # ------------------------------------------------------------------
    # Tick + finish
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._start_monotonic
        if elapsed >= self._duration:
            self._completed = True
            self._finish()
            return
        # Reveal the End early button once the threshold passes.
        if (
            elapsed >= _END_EARLY_REVEAL_SECONDS
            and self._end_early_btn is not None
            and not self._end_early_btn.isVisible()
        ):
            self._end_early_btn.show()
            self._end_early_btn.raise_()
        # Animate the circle's radius from the cycle table.
        cycle_t = elapsed % self._cycle_total
        cursor = 0.0
        phase = self._cycle[0][0]
        ratio = 0.40
        for label, seconds in self._cycle:
            if cycle_t < cursor + seconds:
                local = (cycle_t - cursor) / max(seconds, 1e-3)
                phase = label
                if label == "Inhale":
                    ratio = 0.32 + 0.30 * local  # 0.32 → 0.62
                elif label == "Exhale":
                    ratio = 0.62 - 0.30 * local  # 0.62 → 0.32
                else:  # Hold
                    ratio = 0.62 if cursor > 0 and self._cycle[0][0] == "Inhale" else 0.32
                break
            cursor += seconds
        self._canvas.set_state(ratio, phase)

    def _end_early(self) -> None:
        self._completed = False
        self._finish()

    def _finish(self) -> None:
        if self._already_finished:
            return
        self._already_finished = True
        if self._timer is not None:
            self._timer.stop()
        if self._sound is not None:
            try:
                self._sound.stop()
            except Exception:
                pass
            self._sound = None
        self.releaseKeyboard()
        self.hide()
        if self._loop is not None:
            self._loop.quit()
        elapsed = max(0.0, time.monotonic() - self._start_monotonic)
        try:
            self.finished.emit(elapsed, self._completed)
        except Exception:
            logger.debug("finished signal emit failed", exc_info=True)

    # ------------------------------------------------------------------
    # Events + layout
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            elapsed = time.monotonic() - self._start_monotonic
            if elapsed >= _END_EARLY_REVEAL_SECONDS:
                self._end_early()
                return
            # Within the protective window, the Escape key only flashes
            # the button so the user understands what to look for.
            if self._end_early_btn is not None:
                self._end_early_btn.show()
                self._end_early_btn.raise_()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_children()

    def _layout_children(self) -> None:
        if self._canvas is not None:
            self._canvas.setGeometry(self.rect())
        if self._end_early_btn is not None:
            btn_width = 140
            btn_height = 40
            margin_bottom = 64
            x = (self.width() - btn_width) // 2
            y = self.height() - btn_height - margin_bottom
            self._end_early_btn.setGeometry(x, y, btn_width, btn_height)
            # Keep its current visibility — _tick decides when to reveal.

    # Mathematical helper exposed for unit tests (no Qt mock needed).
    @staticmethod
    def expected_radius_ratio(
        cycle: list[tuple[str, float]],
        elapsed: float,
    ) -> tuple[str, float]:
        """Compute the (phase, radius_ratio) the canvas would display."""
        total = sum(seconds for _, seconds in cycle) or 1.0
        cycle_t = float(elapsed) % total
        cursor = 0.0
        for label, seconds in cycle:
            if cycle_t < cursor + seconds:
                local = (cycle_t - cursor) / max(seconds, 1e-3)
                if label == "Inhale":
                    return label, 0.32 + 0.30 * local
                if label == "Exhale":
                    return label, 0.62 - 0.30 * local
                return label, 0.62
            cursor += seconds
        return cycle[-1][0], 0.32

    # Convenience helper exposed for math-only tests.
    @staticmethod
    def _math_cos(value: float) -> float:
        """Wrapper kept for testability of the easing math (unused inline)."""
        return math.cos(value)


__all__ = [
    "BreakOverlayWindow",
]
