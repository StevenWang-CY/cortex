"""
Desktop Shell — Intervention Overlay

Transparent, always-on-top overlay window that renders LLM-generated
intervention content. Features:

- Semi-transparent backdrop with calming palette (soft blues/whites)
- Headline, situation summary, micro-step checklist
- 4-7-8 breathing pacer animation
- Dismiss via Escape key or close button
- Auto-fade after intervention timeout
"""

from __future__ import annotations

import logging
import math

from PySide6.QtCore import QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# 4-7-8 breathing pattern: inhale 4s, hold 7s, exhale 8s = 19s total cycle
_INHALE_SECONDS = 4
_HOLD_SECONDS = 7
_EXHALE_SECONDS = 8
_CYCLE_SECONDS = _INHALE_SECONDS + _HOLD_SECONDS + _EXHALE_SECONDS

# Warm palette (matching browser extension overlay design)
_BG_COLOR = QColor(12, 12, 14, 224)        # Dark, translucent
_CARD_BG = QColor(30, 30, 34, 240)         # Slightly lighter card
_ACCENT = QColor(217, 119, 87)             # Terracotta #D97757
_TEXT_PRIMARY = QColor(243, 239, 234)       # Warm off-white #F3EFEA
_TEXT_SECONDARY = QColor(153, 149, 144)     # Warm grey #999590
_DISMISS_COLOR = QColor(255, 255, 255, 60)  # Subtle dismiss button


class BreathingPacer(QWidget):
    """
    4-7-8 breathing pacer animation widget.

    Displays an expanding/contracting circle with phase label
    (Inhale, Hold, Exhale) and countdown timer.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._elapsed_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(160, 160)

    def start(self) -> None:
        """Start the breathing animation."""
        self._active = True
        self._elapsed_ms = 0
        self._timer.start()

    def stop(self) -> None:
        """Stop the breathing animation."""
        self._active = False
        self._timer.stop()
        self.update()

    @property
    def is_active(self) -> bool:
        return self._active

    def _tick(self) -> None:
        """Advance animation by one frame."""
        self._elapsed_ms += 33
        self.update()

    def _get_phase(self) -> tuple[str, float, float]:
        """
        Get current breathing phase, progress within phase, and circle scale.

        Returns:
            (phase_label, seconds_remaining, scale 0.0-1.0)
        """
        cycle_pos = (self._elapsed_ms / 1000.0) % _CYCLE_SECONDS

        if cycle_pos < _INHALE_SECONDS:
            progress = cycle_pos / _INHALE_SECONDS
            remaining = _INHALE_SECONDS - cycle_pos
            scale = 0.3 + 0.7 * progress  # Expand
            return "Inhale", remaining, scale

        cycle_pos -= _INHALE_SECONDS
        if cycle_pos < _HOLD_SECONDS:
            remaining = _HOLD_SECONDS - cycle_pos
            return "Hold", remaining, 1.0  # Full

        cycle_pos -= _HOLD_SECONDS
        progress = cycle_pos / _EXHALE_SECONDS
        remaining = _EXHALE_SECONDS - cycle_pos
        scale = 1.0 - 0.7 * progress  # Contract
        return "Exhale", remaining, scale

    def paintEvent(self, event: object) -> None:
        """Paint the breathing circle and labels."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        cx, cy = w // 2, h // 2

        if not self._active:
            painter.setPen(_TEXT_SECONDARY)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Pacer")
            painter.end()
            return

        phase, remaining, scale = self._get_phase()

        # Draw outer ring
        max_radius = min(w, h) // 2 - 10
        radius = int(max_radius * scale)

        # Gradient-like effect with multiple circles
        for i in range(3):
            r = radius - i * 3
            if r < 5:
                break
            alpha = 120 - i * 30
            color = QColor(_ACCENT.red(), _ACCENT.green(), _ACCENT.blue(), alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # Phase label
        painter.setPen(_TEXT_PRIMARY)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRect(0, cy - 20, w, 40),
            Qt.AlignmentFlag.AlignCenter,
            phase,
        )

        # Countdown
        font.setPointSize(10)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(_TEXT_SECONDARY)
        painter.drawText(
            QRect(0, cy + 15, w, 30),
            Qt.AlignmentFlag.AlignCenter,
            f"{remaining:.0f}s",
        )

        painter.end()


class OverlayWindow(QWidget):
    """
    Transparent always-on-top intervention overlay.

    Renders LLM-generated intervention content with calming visuals,
    micro-step checklist, and optional 4-7-8 breathing pacer.

    Signals:
        dismissed(str): Emitted with intervention_id when user dismisses.
    """

    dismissed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._intervention_id = ""

        # Window flags: frameless, always on top, translucent
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setMinimumSize(420, 500)

        # Auto-timeout timer (5 minutes per spec)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(5 * 60 * 1000)  # 5 minutes
        self._timeout_timer.timeout.connect(self._auto_dismiss)

        self._build_ui()

    def _build_ui(self) -> None:
        """Build the overlay UI."""
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(20, 20, 20, 20)

        # Card container
        self._card = QFrame()
        self._card.setStyleSheet(
            f"QFrame {{ background-color: rgba(35, 50, 75, 240); "
            f"border-radius: 16px; }}"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(16)

        # Headline
        self._headline = QLabel("—")
        self._headline.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        self._headline.setStyleSheet(f"color: {_TEXT_PRIMARY.name()};")
        self._headline.setWordWrap(True)
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self._headline)

        # Situation summary
        self._summary = QLabel("")
        self._summary.setFont(QFont("Arial", 13))
        self._summary.setStyleSheet(f"color: {_TEXT_SECONDARY.name()};")
        self._summary.setWordWrap(True)
        card_layout.addWidget(self._summary)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(
            "background-color: rgba(100, 160, 255, 60); max-height: 1px;"
        )
        card_layout.addWidget(divider)

        # Primary focus
        self._focus_label = QLabel("Focus:")
        self._focus_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        self._focus_label.setStyleSheet(f"color: {_ACCENT.name()};")
        self._focus_label.setWordWrap(True)
        card_layout.addWidget(self._focus_label)

        # Micro-steps checklist
        self._steps_container = QVBoxLayout()
        self._steps_container.setSpacing(8)
        self._step_widgets: list[QCheckBox] = []
        card_layout.addLayout(self._steps_container)

        # Breathing pacer
        pacer_layout = QHBoxLayout()
        pacer_layout.addStretch()
        self._pacer = BreathingPacer()
        pacer_layout.addWidget(self._pacer)
        pacer_layout.addStretch()
        card_layout.addLayout(pacer_layout)

        # Dismiss button
        self._dismiss_btn = QPushButton("Dismiss (Esc)")
        self._dismiss_btn.setFont(QFont("Arial", 12))
        self._dismiss_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 40);"
            "  color: white;"
            "  border: 1px solid rgba(255, 255, 255, 60);"
            "  border-radius: 8px;"
            "  padding: 8px 24px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 80);"
            "}"
        )
        self._dismiss_btn.clicked.connect(self._user_dismiss)
        card_layout.addWidget(
            self._dismiss_btn, alignment=Qt.AlignmentFlag.AlignCenter
        )

        self._main_layout.addWidget(self._card)

    def show_intervention(self, payload: dict) -> None:
        """
        Show an intervention overlay with LLM-generated content.

        Args:
            payload: INTERVENTION_TRIGGER payload dict.
        """
        self._intervention_id = payload.get("intervention_id", "")

        # Populate content
        self._headline.setText(payload.get("headline", "Take a moment"))
        self._summary.setText(payload.get("situation_summary", ""))
        self._focus_label.setText(
            f"Focus: {payload.get('primary_focus', '')}"
        )

        # Clear old steps
        for cb in self._step_widgets:
            self._steps_container.removeWidget(cb)
            cb.deleteLater()
        self._step_widgets.clear()

        # Add micro-steps as checkboxes
        for step in payload.get("micro_steps", []):
            cb = QCheckBox(step)
            cb.setFont(QFont("Arial", 12))
            cb.setStyleSheet(
                f"QCheckBox {{ color: {_TEXT_PRIMARY.name()}; spacing: 8px; }}"
                f"QCheckBox::indicator {{ width: 18px; height: 18px; }}"
            )
            self._steps_container.addWidget(cb)
            self._step_widgets.append(cb)

        # Start breathing pacer for overlay_only interventions
        ui_plan = payload.get("ui_plan", {})
        level = payload.get("level", "overlay_only")
        if level == "overlay_only" or ui_plan.get("show_overlay", True):
            self._pacer.start()
            self._pacer.show()
        else:
            self._pacer.stop()
            self._pacer.hide()

        # Position: center of screen
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(min(450, geo.width() - 40), min(600, geo.height() - 40))
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

        # Start timeout timer
        self._timeout_timer.start()

        self.show()
        self.raise_()
        self.activateWindow()

        logger.info(f"Overlay shown for intervention {self._intervention_id}")

    def keyPressEvent(self, event: object) -> None:
        """Handle Escape key to dismiss."""
        from PySide6.QtCore import QEvent

        if hasattr(event, "key") and event.key() == Qt.Key.Key_Escape:
            self._user_dismiss()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: object) -> None:
        """Paint semi-transparent backdrop."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(10, 15, 30, 180))
        painter.end()

    def _user_dismiss(self) -> None:
        """Handle user dismissal."""
        self._timeout_timer.stop()
        self._pacer.stop()
        self.hide()
        self.dismissed.emit(self._intervention_id)
        logger.info(f"Intervention {self._intervention_id} dismissed by user")

    def _auto_dismiss(self) -> None:
        """Handle auto-timeout dismissal (5 min)."""
        self._pacer.stop()
        self.hide()
        self.dismissed.emit(self._intervention_id)
        logger.info(f"Intervention {self._intervention_id} auto-dismissed (timeout)")
