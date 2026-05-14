"""Desktop Shell — Dashboard Window (macOS-native refactor).

Two-tab layout:
    Tab 1 "Dashboard" — Consumer biometrics view (Cormorant numerics, terracotta
                        accent, native typography & spacing)
    Tab 2 "Advanced"  — Developer debug view: HR trace, signal quality, scores

The visual layer is now driven by:

* :mod:`cortex.apps.desktop_shell.tokens` (emitted from
  ``cortex/libs/design/tokens.yaml``) — semantic palette, 5-step type scale,
  HIG-compliant spacing & radii.
* :mod:`cortex.apps.desktop_shell.mac_native` — system font, NSVisualEffectView
  vibrancy, unified title bar. Brand identity (terracotta accent +
  Cormorant Garamond wordmark/numerics + ECG heartbeat motif) is preserved on
  top of native materials.

All public Signals, slots, and update methods are byte-identical to the
pre-refactor implementation so :mod:`cortex.apps.desktop_shell.controller`
and :mod:`cortex.apps.desktop_shell.main` do not need to change.
"""

from __future__ import annotations

import collections
import logging
import time

from PySide6.QtCore import Qt, Signal

try:
    from PySide6.QtCore import QRectF
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtCore import QRect as QRectF
try:
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtGui import QColor, QFont, QPainter, QPen

    class QPainterPath:  # type: ignore[override]
        def addRoundedRect(self, *_args: object, **_kwargs: object) -> None:
            return

        def moveTo(self, *_args: object, **_kwargs: object) -> None:
            return

        def lineTo(self, *_args: object, **_kwargs: object) -> None:
            return
try:
    from PySide6.QtWidgets import (
        QButtonGroup,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtWidgets import (  # type: ignore[attr-defined]
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class QButtonGroup:  # type: ignore[override]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def addButton(self, *_args: object, **_kwargs: object) -> None:
            return

        def setExclusive(self, *_args: object, **_kwargs: object) -> None:
            return

    class QLineEdit(QLabel):  # type: ignore[override]
        def setPlaceholderText(self, *_args: object, **_kwargs: object) -> None:
            return

    class QScrollArea(QWidget):  # type: ignore[override]
        def setWidgetResizable(self, *_args: object, **_kwargs: object) -> None:
            return

        def setWidget(self, *_args: object, **_kwargs: object) -> None:
            return

    class QSizePolicy:  # type: ignore[override]
        class Policy:
            Expanding = 0
            Preferred = 0

    class QStackedWidget(QWidget):  # type: ignore[override]
        def addWidget(self, *_args: object, **_kwargs: object) -> None:
            return

        def setCurrentIndex(self, *_args: object, **_kwargs: object) -> None:
            return

# Tab widget compatibility shim retained for test harness even though the new
# dashboard uses a segmented control + QStackedWidget. Some downstream tests
# still reference QTabWidget at import time.
try:
    from PySide6.QtWidgets import QTabWidget  # noqa: F401 - re-exported
except ImportError:  # pragma: no cover
    pass

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BIO_BLINK,
    BIO_HR,
    BIO_HRV,
    BRAND_ACCENT,
    BRAND_DISPLAY_FONT,
    DASHBOARD_MAX_HEIGHT,
    DASHBOARD_WIDTH,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_HERO_NUMERIC,
    FS_TITLE,
    FW_REGULAR,
    FW_SEMIBOLD,
    RADIUS_CARD,
    RADIUS_PILL,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
    STATE_COLORS,
    STATE_LABELS,
)

logger = logging.getLogger(__name__)

_MAX_HR_HISTORY = 120
_MAX_TIMELINE_EVENTS = 50

# Resolved semantic colors. These hex strings are dev-mode fallbacks; on
# macOS, ``mac_native`` re-tints widgets at runtime when the user toggles
# light/dark mode (see :func:`mac_native.install_appearance_observer`).
_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = "#5C5854"   # high-contrast secondary (AA passes on warm bg)
_LABEL_TERTIARY = "#827971"    # AA-passing tertiary (placeholders, captions)
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_DANGER = SEMANTIC_LIGHT["danger"]


def _system(point_size: float, weight: str = "regular") -> str:
    """Return a Qt stylesheet font-family value resolving to the system font.

    Used inside QSS strings where a literal stack is required. The companion
    helper :func:`mac_native.system_font` returns an actual ``QFont`` for use
    with ``setFont()`` calls.
    """
    return '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif'


# ---------------------------------------------------------------------------
# Global stylesheet — minimal, semantic
# ---------------------------------------------------------------------------

_GLOBAL_QSS = f"""
QWidget#CortexDashboard {{
    background-color: transparent;
}}
QLineEdit {{
    selection-background-color: {BRAND_ACCENT};
}}
QToolTip {{
    background-color: {_CONTROL_BG};
    color: {_LABEL};
    border: 1px solid {_SEPARATOR};
    padding: 4px 8px;
    border-radius: 6px;
}}
"""


# ---------------------------------------------------------------------------
# Native-style segmented control (capsule pill, two segments)
# ---------------------------------------------------------------------------

class _MacSegmentedControl(QWidget):
    """Two-segment capsule pill matching ``NSSegmentedControl.capsule`` look.

    Emits ``selection_changed(int)`` when the user clicks a segment. Used in
    place of the previous ``QTabWidget`` underline-accent bar (which is a
    Chrome/Material pattern, not a Mac one).
    """

    selection_changed = Signal(int)

    def __init__(self, labels: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        track = QFrame()
        track.setObjectName("_seg_track")
        track.setStyleSheet(
            f"#_seg_track {{ background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: 8px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(3, 3, 3, 3)
        track_layout.setSpacing(2)
        for index, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            btn.setStyleSheet(
                "QPushButton {"
                "  padding: 4px 14px;"
                "  border-radius: 6px;"
                "  background: transparent;"
                f"  color: {_LABEL_SECONDARY};"
                "  border: none;"
                "}"
                f"QPushButton:hover {{ color: {_LABEL}; }}"
                "QPushButton:checked {"
                f"  background: {_CONTROL_BG};"
                f"  color: {_LABEL};"
                f"  font-weight: {FW_SEMIBOLD};"
                "}"
            )
            btn.clicked.connect(lambda _checked=False, i=index: self._on_clicked(i))
            self._group.addButton(btn, index)
            self._buttons.append(btn)
            track_layout.addWidget(btn, stretch=1)
        outer.addWidget(track, stretch=1)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, index: int) -> None:
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)
        self.selection_changed.emit(index)


# ---------------------------------------------------------------------------
# Tab 1: Consumer Dashboard
# ---------------------------------------------------------------------------

class _ConsumerTab(QWidget):
    """Clean biometrics dashboard — native materials, brand identity intact."""

    # E.1: surface user intent for the daemon orchestrator. The shell only
    # owns the widgets; the parent dashboard re-emits these signals so the
    # desktop app (in-process or WebSocket mode) can route them to
    # ``RuntimeDaemon._handle_user_action`` and to ``_shutdown_daemon``.
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {_LABEL};")

        root = QVBoxLayout(self)
        root.setContentsMargins(SP6, SP5, SP6, SP6)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, SP5)

        # Brand wordmark (preserved — Cormorant italic, terracotta is the
        # signature contrast). HIG section-heading conventions don't apply to
        # the wordmark; it's the app identity.
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-style: italic; font-size: {FS_TITLE}px;"
            f"font-weight: {FW_REGULAR};"
            f"color: {_LABEL}; background: transparent;"
        )
        header.addWidget(brand)
        header.addStretch()

        # State pill — capsule with dot + label, sits on the grouped background.
        self._state_badge = QWidget()
        badge_layout = QHBoxLayout(self._state_badge)
        badge_layout.setContentsMargins(10, 3, 12, 3)
        badge_layout.setSpacing(6)

        self._state_dot = QLabel()
        self._state_dot.setFixedSize(7, 7)
        self._state_dot.setStyleSheet(
            f"background: {_LABEL_TERTIARY}; border-radius: 3px;"
        )
        badge_layout.addWidget(self._state_dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_label = QLabel("Disconnected")
        self._state_label.setFont(mac_native.system_font(FS_FOOTNOTE - 1, "medium"))
        self._state_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        badge_layout.addWidget(self._state_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_badge.setStyleSheet(
            f"background: {_GROUPED_BG}; border-radius: {RADIUS_PILL}px;"
        )
        header.addWidget(self._state_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        # ── Goal input — minimum width, flexible (HIG: avoid fixed sizes) ──
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setMinimumHeight(36)
        self._goal_input.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._goal_input.setStyleSheet(
            "QLineEdit {"
            f"  padding: 0 {SP4}px;"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: 6px;"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            "}"
            f"QLineEdit:focus {{ border: 1.5px solid {BRAND_ACCENT}; }}"
            f"QLineEdit::placeholder {{ color: {_LABEL_TERTIARY}; }}"
        )
        # E.1: emit goal_set when the user hits return.
        self._goal_input.returnPressed.connect(
            lambda: self.goal_set.emit(self._goal_input.text().strip())
        )
        root.addWidget(self._goal_input)
        root.addSpacing(SP5)

        # ── Biometrics inset section (no shadow, hairline border) ──
        bio_card = QFrame()
        bio_card.setStyleSheet(
            f"QFrame {{"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}}"
        )
        bio_inner = QVBoxLayout(bio_card)
        bio_inner.setContentsMargins(SP5, SP4, SP5, SP4)
        bio_inner.setSpacing(SP3)

        # Sentence-case section heading (HIG) — no letter-spacing, secondary color.
        bio_heading = QLabel("Biometrics")
        bio_heading.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        bio_heading.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        bio_inner.addWidget(bio_heading)

        bio_row = QHBoxLayout()
        bio_row.setSpacing(0)

        self._bpm_label = QLabel("--")
        self._hrv_label = QLabel("--")
        self._blk_label = QLabel("--")

        for val_widget, title, color in [
            (self._bpm_label, "BPM", BIO_HR),
            (self._hrv_label, "HRV", BIO_HRV),
            (self._blk_label, "BLK", BIO_BLINK),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Brand identity — Cormorant numerics, terracotta channel
            # accents — preserved across the macOS refactor.
            val_widget.setStyleSheet(
                f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
                f"font-size: {FS_HERO_NUMERIC}px;"
                f"font-weight: {FW_REGULAR};"
                f"color: {_LABEL};"
                f"background: transparent; border: none;"
            )

            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            heading.setStyleSheet(
                f"color: {color}; background: transparent; border: none;"
            )
            col.addWidget(val_widget)
            col.addWidget(heading)
            bio_row.addLayout(col, stretch=1)

        bio_inner.addLayout(bio_row)
        root.addWidget(bio_card)
        root.addSpacing(SP4)

        # ── Connections row ───────────────────────────────────────────
        conn_row = QHBoxLayout()
        conn_row.setContentsMargins(SP2, 0, SP2, 0)
        conn_row.setSpacing(SP4)

        self._conn_dots: dict[str, QLabel] = {}
        for name in ("Chrome", "Edge", "Editor"):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(
                f"background: {_LABEL_TERTIARY}; border-radius: 3px;"
            )
            lbl = QLabel(name)
            lbl.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            lbl.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            conn_row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)
            conn_row.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
            self._conn_dots[name] = dot

        conn_row.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._connect_btn.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._connect_btn.setStyleSheet(
            "QPushButton {"
            f"  color: {BRAND_ACCENT};"
            f"  background: transparent;"
            f"  border: none;"
            f"  padding: 4px 0;"
            "}"
            f"QPushButton:hover {{ color: {_LABEL}; }}"
        )
        conn_row.addWidget(self._connect_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(conn_row)
        root.addSpacing(SP5)

        # ── Divider (hairline, system separator) ───────────────────────
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {_SEPARATOR};")
        root.addWidget(divider)
        root.addSpacing(SP5)

        # ── Today stats — sentence-case, no letter-spacing ────────────
        today_label = QLabel("Today")
        today_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        today_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        root.addWidget(today_label)
        root.addSpacing(SP3)

        today_row = QHBoxLayout()
        today_row.setSpacing(0)

        self._today_focus = QLabel("--")
        self._today_sessions = QLabel("--")
        self._today_best = QLabel("--")
        self._today_blocked = QLabel("--")

        for val_widget, title in [
            (self._today_focus, "Focus"),
            (self._today_sessions, "Sessions"),
            (self._today_best, "Best"),
            (self._today_blocked, "Blocked"),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setStyleSheet(
                f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
                f"font-size: {FS_TITLE}px;"
                f"color: {_LABEL};"
                f"background: transparent;"
            )
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            heading.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            col.addWidget(val_widget)
            col.addWidget(heading)
            today_row.addLayout(col, stretch=1)

        root.addLayout(today_row)
        root.addStretch()

        # ── Stop button (HIG destructive role) ─────────────────────────
        root.addSpacing(SP4)
        self._stop_btn = QPushButton("Stop Cortex")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setMinimumHeight(36)  # HIG tap target ≥ 44 once font padding factored
        self._stop_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._stop_btn.setShortcut("Ctrl+Q")  # VoiceOver picks this up
        self._stop_btn.setAccessibleName("Stop Cortex")
        self._stop_btn.setStyleSheet(
            "QPushButton {"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  background: {_CONTROL_BG};"
            f"  color: {_DANGER};"
            f"  border-radius: 8px;"
            f"  padding: 6px 14px;"
            "}"
            f"QPushButton:hover {{ background: rgba(215, 0, 21, 0.06); }}"
            f"QPushButton:pressed {{ background: rgba(215, 0, 21, 0.12); }}"
        )
        # E.1: emit stop_requested so the parent dashboard re-emits and the
        # app-level handler calls _shutdown_daemon.
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        root.addWidget(self._stop_btn)

    # -- Public update methods (preserved byte-identical) ----------------

    def update_state(self, payload: dict) -> None:
        state = payload.get("state", "FLOW")
        color = STATE_COLORS.get(state, _LABEL_TERTIARY)
        label = STATE_LABELS.get(state, state)
        self._state_dot.setStyleSheet(
            f"background: {color}; border-radius: 3px;"
        )
        self._state_label.setText(label)
        self._state_label.setStyleSheet(
            f"color: {color}; background: transparent;"
        )

        bio = payload.get("biometrics", {})
        hr = bio.get("heart_rate")
        hrv = bio.get("hrv_rmssd")
        blink = bio.get("blink_rate")
        self._bpm_label.setText(f"{hr:.0f}" if hr is not None else "--")
        self._hrv_label.setText(f"{hrv:.0f}" if hrv is not None else "--")
        self._blk_label.setText(f"{blink:.1f}" if blink is not None else "--")

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._state_label.setText("Connected")
            self._state_dot.setStyleSheet(
                f"background: {BRAND_ACCENT}; border-radius: 3px;"
            )
        else:
            self._state_label.setText("Disconnected")
            self._state_dot.setStyleSheet(
                f"background: {_LABEL_TERTIARY}; border-radius: 3px;"
            )


# ---------------------------------------------------------------------------
# HR Trace Plot — brand accent trace, system separator grid
# ---------------------------------------------------------------------------

class HRTracePlot(QWidget):
    """Rolling HR trace. Grid lines use the system separator color; the trace
    itself is the brand accent (terracotta) — the ECG identity preserved."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: collections.deque[float] = collections.deque(maxlen=_MAX_HR_HISTORY)
        self.setMinimumHeight(120)
        self.setMinimumWidth(300)

    def add_value(self, hr: float) -> None:
        self._values.append(hr)
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = 8

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_CONTROL_BG))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS_CARD, RADIUS_CARD)
        painter.drawPath(path)

        painter.setPen(QPen(QColor(0, 0, 0, 24), 1))  # ~ system separator 15%
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        if len(self._values) < 2:
            painter.setPen(QColor(_LABEL_TERTIARY))
            painter.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for HR data...")
            painter.end()
            return

        min_hr = max(40.0, min(self._values) - 5)
        max_hr = min(180.0, max(self._values) + 5)
        hr_range = max(max_hr - min_hr, 10.0)

        painter.setPen(QPen(QColor(0, 0, 0, 12), 1))  # ~ tertiary label
        for tick in range(int(min_hr), int(max_hr) + 1, 10):
            y = pad + (h - 2 * pad) - int((tick - min_hr) / hr_range * (h - 2 * pad))
            painter.drawLine(pad, y, w - pad, y)

        pen = QPen(QColor(BRAND_ACCENT), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        vals = list(self._values)
        n = len(vals)
        for i in range(1, n):
            x1 = pad + int((i - 1) / max(n - 1, 1) * (w - 2 * pad))
            x2 = pad + int(i / max(n - 1, 1) * (w - 2 * pad))
            y1 = pad + (h - 2 * pad) - int((vals[i - 1] - min_hr) / hr_range * (h - 2 * pad))
            y2 = pad + (h - 2 * pad) - int((vals[i] - min_hr) / hr_range * (h - 2 * pad))
            painter.drawLine(x1, y1, x2, y2)

        painter.setPen(QColor(_LABEL))
        f = mac_native.system_font(FS_FOOTNOTE, "semibold")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.drawText(w - 80, h - 12, f"{vals[-1]:.0f} BPM")

        painter.end()


# ---------------------------------------------------------------------------
# Signal quality bar
# ---------------------------------------------------------------------------

class _SignalQualityBar(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self._label = QLabel(label)
        self._label.setFixedWidth(76)
        self._label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {_GROUPED_BG};"
            f" border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {BRAND_ACCENT};"
            f" border-radius: 2px; }}"
        )
        layout.addWidget(self._bar)

        self._val_label = QLabel("0%")
        self._val_label.setFixedWidth(36)
        self._val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._val_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._val_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        layout.addWidget(self._val_label)

    def set_value(self, quality: float) -> None:
        pct = int(quality * 100)
        self._bar.setValue(pct)
        self._val_label.setText(f"{pct}%")
        if quality >= 0.7:
            color = SEMANTIC_LIGHT["success"]
        elif quality >= 0.4:
            color = BIO_BLINK
        else:
            color = _DANGER
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {_GROUPED_BG};"
            f" border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {color};"
            f" border-radius: 2px; }}"
        )


# ---------------------------------------------------------------------------
# Tab 2: Advanced
# ---------------------------------------------------------------------------

class _AdvancedTab(QWidget):
    """Developer debug view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {_LABEL};")
        self._timeline_events: list[dict] = []
        self._session_start = time.monotonic()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP4)

        sq_label = QLabel("Signal quality")
        sq_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        sq_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(sq_label)

        self._physio_q = _SignalQualityBar("Physio")
        self._kine_q = _SignalQualityBar("Kinematics")
        self._tele_q = _SignalQualityBar("Telemetry")
        layout.addWidget(self._physio_q)
        layout.addWidget(self._kine_q)
        layout.addWidget(self._tele_q)
        layout.addSpacing(SP2)

        hr_label = QLabel("Heart rate")
        hr_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        hr_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(hr_label)
        self._hr_plot = HRTracePlot()
        layout.addWidget(self._hr_plot)

        scores_label = QLabel("State scores")
        scores_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        scores_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(scores_label)

        scores_grid = QGridLayout()
        scores_grid.setVerticalSpacing(6)
        self._score_bars: dict[str, QProgressBar] = {}
        self._score_labels: dict[str, QLabel] = {}
        for i, (name, color) in enumerate([
            ("flow", STATE_COLORS["FLOW"]),
            ("hyper", STATE_COLORS["HYPER"]),
            ("hypo", STATE_COLORS["HYPO"]),
            ("recovery", STATE_COLORS["RECOVERY"]),
        ]):
            lbl = QLabel(name.capitalize())
            lbl.setFixedWidth(72)
            lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            lbl.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            scores_grid.addWidget(lbl, i, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(5)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {_GROUPED_BG}; border: none;"
                f" border-radius: 2px; }}"
                f"QProgressBar::chunk {{ background: {color};"
                f" border-radius: 2px; }}"
            )
            scores_grid.addWidget(bar, i, 1)
            val_lbl = QLabel("0.00")
            val_lbl.setFixedWidth(36)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_lbl.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            val_lbl.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            scores_grid.addWidget(val_lbl, i, 2)
            self._score_bars[name] = bar
            self._score_labels[name] = val_lbl
        layout.addLayout(scores_grid)

        meta_row = QHBoxLayout()
        self._confidence_lbl = QLabel("Confidence: --")
        self._confidence_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._confidence_lbl.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._dwell_lbl = QLabel("Dwell: --")
        self._dwell_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._dwell_lbl.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        meta_row.addWidget(self._confidence_lbl)
        meta_row.addStretch()
        meta_row.addWidget(self._dwell_lbl)
        layout.addLayout(meta_row)

        tl_label = QLabel("Timeline")
        tl_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        tl_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(tl_label)
        self._timeline_text = QLabel("No events yet")
        self._timeline_text.setWordWrap(True)
        self._timeline_text.setStyleSheet(
            f"font-family: \"SF Mono\", ui-monospace, Menlo, monospace;"
            f"font-size: {FS_CAPTION}px; color: {_LABEL_SECONDARY};"
            f"background: transparent; line-height: 1.6;"
        )
        self._timeline_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._timeline_text)
        layout.addStretch()

    def update_state(self, payload: dict) -> None:
        scores = payload.get("scores", {})
        sig_q = payload.get("signal_quality", {})
        confidence = payload.get("confidence", 0.0)
        dwell = payload.get("dwell_seconds", 0.0)
        state = payload.get("state", "FLOW")
        bio = payload.get("biometrics", {})

        self._physio_q.set_value(sig_q.get("physio", 0.0))
        self._kine_q.set_value(sig_q.get("kinematics", 0.0))
        self._tele_q.set_value(sig_q.get("telemetry", 0.0))

        hr = bio.get("heart_rate")
        if hr is not None:
            self._hr_plot.add_value(hr)

        for name in ("flow", "hyper", "hypo", "recovery"):
            val = scores.get(name, 0.0)
            if name in self._score_bars:
                self._score_bars[name].setValue(int(val * 100))
                self._score_labels[name].setText(f"{val:.2f}")

        self._confidence_lbl.setText(f"Confidence: {confidence:.0%}")
        self._dwell_lbl.setText(f"Dwell: {dwell:.1f}s")

        if not self._timeline_events or self._timeline_events[-1]["state"] != state:
            elapsed = time.monotonic() - self._session_start
            self._timeline_events.append({
                "time": elapsed, "state": state, "confidence": confidence,
            })
            if len(self._timeline_events) > _MAX_TIMELINE_EVENTS:
                self._timeline_events = self._timeline_events[-_MAX_TIMELINE_EVENTS:]
            lines = []
            for ev in reversed(self._timeline_events[-8:]):
                t = ev["time"]
                m, s = int(t // 60), t % 60
                lines.append(f"{m:02d}:{s:04.1f}  {ev['state']:<10} {ev['confidence']:.0%}")
            self._timeline_text.setText("\n".join(lines) if lines else "No events yet")


# ---------------------------------------------------------------------------
# Main Dashboard Window
# ---------------------------------------------------------------------------

class DashboardWindow(QWidget):
    """Two-tab dashboard with native chrome.

    Uses a segmented control + stacked widget instead of QTabWidget — the
    macOS convention for two-segment top-level navigation.
    """

    # E.1: re-emit user-intent signals from the consumer tab.
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self.setObjectName("CortexDashboard")
        self.setWindowTitle("Cortex")
        # HIG: minimum width, flexible. Macs at 1024×768 still fit comfortably.
        self.setMinimumWidth(DASHBOARD_WIDTH)
        self.setMaximumWidth(DASHBOARD_WIDTH + 60)
        self.setMaximumHeight(DASHBOARD_MAX_HEIGHT)
        self.setStyleSheet(_GLOBAL_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Segmented control sits at the top under the unified title bar.
        seg_container = QHBoxLayout()
        seg_container.setContentsMargins(SP6, SP3, SP6, SP3)
        self._seg = _MacSegmentedControl(["Dashboard", "Advanced"])
        seg_container.addWidget(self._seg, stretch=1)
        layout.addLayout(seg_container)

        self._stack = QStackedWidget()
        self._consumer = _ConsumerTab()
        self._advanced = _AdvancedTab()
        self._timeline_events = self._advanced._timeline_events
        self._stack.addWidget(self._consumer)
        self._stack.addWidget(self._advanced)
        layout.addWidget(self._stack, stretch=1)

        self._seg.selection_changed.connect(self._stack.setCurrentIndex)

        # E.1: forward consumer-tab signals to outer subscribers.
        self._consumer.stop_requested.connect(self.stop_requested.emit)
        self._consumer.goal_set.connect(self.goal_set.emit)

    # -- Lifecycle hook for native chrome --------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        # Apply native materials once winId() is valid. Re-applying on each
        # show is cheap and idempotent.
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="window_background")
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)

    # -- Public update methods (signature-stable) ------------------------

    def update_state(self, payload: dict) -> None:
        self._consumer.update_state(payload)
        self._advanced.update_state(payload)

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._consumer.set_connected(connected)
