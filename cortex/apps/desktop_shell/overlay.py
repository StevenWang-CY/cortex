"""Desktop Shell — Intervention Overlay (HUD-vibrancy refactor).

A frameless, always-on-top panel that renders LLM-generated intervention
content. On macOS the window is backed by ``NSVisualEffectMaterialHUDWindow``
(the same dark vibrancy used by Spotlight / Notification Center), so the
overlay feels like a native HUD instead of a custom-drawn dark rectangle.

The terracotta brand accent + breathing-pacer animation + causal_explanation
row are all preserved from the previous implementation. ``BreathingPacer``'s
geometry math is unchanged.

Public API: ``dismissed(str)`` Signal, ``show_intervention(payload)`` method.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_DISPLAY_FONT,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_REGULAR,
    RADIUS_BUTTON,
    RADIUS_WINDOW,
    SP3,
    SP4,
    SP6,
    SP8,
)

logger = logging.getLogger(__name__)

# 4-7-8 breathing pattern: inhale 4s, hold 7s, exhale 8s = 19s total cycle.
_INHALE_SECONDS = 4
_HOLD_SECONDS = 7
_EXHALE_SECONDS = 8
_CYCLE_SECONDS = _INHALE_SECONDS + _HOLD_SECONDS + _EXHALE_SECONDS

# HUD palette — the only hardcoded colors in the file. The vibrancy view
# below the window provides the actual dark blur; these colors are how the
# overlay's content layers itself on top of that material.
_ACCENT = QColor(217, 119, 87)              # Terracotta #D97757 (brand)
_TEXT_PRIMARY = QColor(255, 255, 255, 235)  # SF system "labelColor" on HUD
_TEXT_SECONDARY = QColor(255, 255, 255, 150)
_TEXT_TERTIARY = QColor(255, 255, 255, 100)


class BreathingPacer(QWidget):
    """4-7-8 breathing pacer animation widget. Geometry unchanged from prior
    revision; only label fonts swap to the SF system stack."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._elapsed_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(160, 160)

    def start(self) -> None:
        self._active = True
        self._elapsed_ms = 0
        self._timer.start()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self.update()

    @property
    def is_active(self) -> bool:
        return self._active

    def _tick(self) -> None:
        self._elapsed_ms += 33
        self.update()

    def _get_phase(self) -> tuple[str, float, float]:
        cycle_pos = (self._elapsed_ms / 1000.0) % _CYCLE_SECONDS
        if cycle_pos < _INHALE_SECONDS:
            progress = cycle_pos / _INHALE_SECONDS
            remaining = _INHALE_SECONDS - cycle_pos
            scale = 0.3 + 0.7 * progress
            return "Inhale", remaining, scale
        cycle_pos -= _INHALE_SECONDS
        if cycle_pos < _HOLD_SECONDS:
            remaining = _HOLD_SECONDS - cycle_pos
            return "Hold", remaining, 1.0
        cycle_pos -= _HOLD_SECONDS
        progress = cycle_pos / _EXHALE_SECONDS
        remaining = _EXHALE_SECONDS - cycle_pos
        scale = 1.0 - 0.7 * progress
        return "Exhale", remaining, scale

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        cx, cy = w // 2, h // 2

        if not self._active:
            painter.setPen(_TEXT_SECONDARY)
            f = mac_native.system_font(FS_FOOTNOTE, "regular")
            if isinstance(f, QFont):
                painter.setFont(f)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Pacer")
            painter.end()
            return

        phase, remaining, scale = self._get_phase()

        max_radius = min(w, h) // 2 - 10
        radius = int(max_radius * scale)
        for i in range(3):
            r = radius - i * 3
            if r < 5:
                break
            alpha = 100 - i * 25
            color = QColor(_ACCENT.red(), _ACCENT.green(), _ACCENT.blue(), alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        painter.setPen(_TEXT_PRIMARY)
        f = mac_native.system_font(FS_BODY, "semibold")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.drawText(
            QRect(0, cy - 20, w, 40),
            Qt.AlignmentFlag.AlignCenter,
            phase,
        )

        f = mac_native.system_font(FS_CAPTION, "regular")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.setPen(_TEXT_SECONDARY)
        painter.drawText(
            QRect(0, cy + 15, w, 30),
            Qt.AlignmentFlag.AlignCenter,
            f"{remaining:.0f}s",
        )

        painter.end()


class OverlayWindow(QWidget):
    """Frameless always-on-top intervention overlay backed by HUD vibrancy."""

    dismissed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._intervention_id = ""

        # Frameless + always-on-top. Translucent background lets the
        # NSVisualEffectView under the window show through.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setMinimumSize(440, 520)

        # Auto-timeout — 5 min per spec.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(5 * 60 * 1000)
        self._timeout_timer.timeout.connect(self._auto_dismiss)

        self._build_ui()

    # -- Lifecycle hook: apply HUD vibrancy ------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="hudWindow")
        except Exception:
            pass

    def _build_ui(self) -> None:
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(24, 24, 24, 24)

        # Card — translucent dark surface that layers on top of the HUD
        # vibrancy material below. The 6% white border picks out the card
        # edge against the blur.
        self._card = QFrame()
        self._card.setStyleSheet(
            "QFrame {"
            "  background-color: rgba(30, 30, 32, 0.55);"
            f"  border-radius: {RADIUS_WINDOW}px;"
            "  border: 0.5px solid rgba(255, 255, 255, 0.10);"
            "}"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(SP8, SP6, SP8, SP6)
        card_layout.setSpacing(SP4)

        # Headline — Cormorant display (brand-preserved).
        self._headline = QLabel("—")
        self._headline.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-size: {FS_TITLE}px;"
            f"font-weight: {FW_REGULAR};"
            "font-style: italic;"
            f"color: {_TEXT_PRIMARY.name()};"
            "background: transparent;"
        )
        self._headline.setWordWrap(True)
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self._headline)

        # Situation summary — SF system, FN-size, secondary alpha.
        self._summary = QLabel("")
        self._summary.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._summary.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()};"
            "background: transparent; line-height: 1.5;"
        )
        self._summary.setWordWrap(True)
        card_layout.addWidget(self._summary)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(
            "background-color: rgba(255, 255, 255, 0.10); max-height: 1px;"
        )
        card_layout.addWidget(divider)

        # Primary focus.
        self._focus_label = QLabel("Focus:")
        self._focus_label.setFont(mac_native.system_font(FS_BODY, "semibold"))
        self._focus_label.setStyleSheet(
            f"color: {_ACCENT.name()}; background: transparent;"
        )
        self._focus_label.setWordWrap(True)
        card_layout.addWidget(self._focus_label)

        # Micro-steps checklist.
        self._steps_container = QVBoxLayout()
        self._steps_container.setSpacing(SP3)
        self._step_widgets: list[QCheckBox] = []
        card_layout.addLayout(self._steps_container)

        # "Why this?" causal explanation — surfaces only when supplied.
        self._causal_label = QLabel("")
        self._causal_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._causal_label.setStyleSheet(
            f"color: {_TEXT_TERTIARY.name()};"
            "background: transparent;"
            "font-style: italic;"
        )
        self._causal_label.setWordWrap(True)
        self._causal_label.hide()
        card_layout.addWidget(self._causal_label)

        # Breathing pacer.
        pacer_layout = QHBoxLayout()
        pacer_layout.addStretch()
        self._pacer = BreathingPacer()
        pacer_layout.addWidget(self._pacer)
        pacer_layout.addStretch()
        card_layout.addLayout(pacer_layout)

        # Dismiss button — HUD-style capsule.
        self._dismiss_btn = QPushButton("Dismiss (Esc)")
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._dismiss_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.08);"
            "  color: rgba(255, 255, 255, 0.85);"
            "  border: 0.5px solid rgba(255, 255, 255, 0.14);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 8px 22px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.16);"
            "  color: white;"
            "}"
        )
        self._dismiss_btn.clicked.connect(self._user_dismiss)
        card_layout.addWidget(
            self._dismiss_btn, alignment=Qt.AlignmentFlag.AlignCenter
        )

        self._main_layout.addWidget(self._card)

    # ------------------------------------------------------------------
    # Public API (preserved byte-identical)
    # ------------------------------------------------------------------

    def show_intervention(self, payload: dict) -> None:
        self._intervention_id = payload.get("intervention_id", "")

        self._headline.setText(payload.get("headline", "Take a moment"))
        self._summary.setText(payload.get("situation_summary", ""))
        self._focus_label.setText(
            f"Focus: {payload.get('primary_focus', '')}"
        )

        for cb in self._step_widgets:
            self._steps_container.removeWidget(cb)
            cb.deleteLater()
        self._step_widgets.clear()

        causal = str(payload.get("causal_explanation") or "").strip()
        if causal and len(causal) > 20:
            self._causal_label.setText(f"Why this? {causal}")
            self._causal_label.show()
        else:
            self._causal_label.setText("")
            self._causal_label.hide()

        for step in payload.get("micro_steps", []):
            cb = QCheckBox(step)
            cb.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            cb.setStyleSheet(
                "QCheckBox {"
                f"  color: {_TEXT_PRIMARY.name()};"
                "  spacing: 10px;"
                "  background: transparent;"
                "}"
                "QCheckBox::indicator { width: 16px; height: 16px; }"
            )
            self._steps_container.addWidget(cb)
            self._step_widgets.append(cb)

        ui_plan = payload.get("ui_plan", {})
        level = payload.get("level", "overlay_only")
        if level == "overlay_only" or ui_plan.get("show_overlay", True):
            self._pacer.start()
            self._pacer.show()
        else:
            self._pacer.stop()
            self._pacer.hide()

        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(min(460, geo.width() - 40), min(620, geo.height() - 40))
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

        self._timeout_timer.start()

        self.show()
        self.raise_()
        self.activateWindow()

        logger.info(f"Overlay shown for intervention {self._intervention_id}")

    def keyPressEvent(self, event: object) -> None:
        if hasattr(event, "key") and event.key() == Qt.Key.Key_Escape:
            self._user_dismiss()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: object) -> None:
        # The NSVisualEffectView provides the actual blur. We paint only
        # a very faint translucent scrim on top so the card edge reads
        # cleanly against bright wallpapers; on Linux/Windows fallback
        # this is what gives the dim effect.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not mac_native.is_macos():
            painter.fillRect(self.rect(), QColor(10, 12, 20, 200))
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 30))
        painter.end()

    def _user_dismiss(self) -> None:
        self._timeout_timer.stop()
        self._pacer.stop()
        self.hide()
        self.dismissed.emit(self._intervention_id)
        logger.info(f"Intervention {self._intervention_id} dismissed by user")

    def _auto_dismiss(self) -> None:
        self._pacer.stop()
        self.hide()
        self.dismissed.emit(self._intervention_id)
        logger.info(
            f"Intervention {self._intervention_id} auto-dismissed (timeout)"
        )
