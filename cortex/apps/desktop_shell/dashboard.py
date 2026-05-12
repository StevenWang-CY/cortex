"""
Desktop Shell — Dashboard Window

Two-tab layout:
  Tab 1 "Dashboard" — Clean biometrics view matching the browser extension popup
  Tab 2 "Advanced"  — Developer debug view with HR trace, signal quality, state scores
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
        QFrame,
        QGraphicsDropShadowEffect,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QTabWidget,
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

    class QGraphicsDropShadowEffect:  # type: ignore[override]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def setBlurRadius(self, *_args: object, **_kwargs: object) -> None:
            return

        def setOffset(self, *_args: object, **_kwargs: object) -> None:
            return

        def setColor(self, *_args: object, **_kwargs: object) -> None:
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

    class QTabWidget(QWidget):  # type: ignore[override]
        def addTab(self, *_args: object, **_kwargs: object) -> None:
            return

from cortex.apps.desktop_shell.tokens import (
    CARD_QSS,
    CX_ACCENT,
    CX_BG,
    CX_BIO_BLINK,
    CX_BIO_HR,
    CX_BIO_HRV,
    CX_BORDER,
    CX_BORDER_DEFAULT,
    CX_DANGER,
    CX_FONT_BRAND,
    CX_FONT_MONO,
    CX_FONT_SANS,
    CX_SURFACE,
    CX_TERTIARY,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DASHBOARD_MAX_HEIGHT,
    DASHBOARD_WIDTH,
    RADIUS_FULL,
    RADIUS_MD,
    RADIUS_SM,
    SECTION_HEADING_QSS,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card_shadow(widget: QWidget) -> None:
    """Apply a soft drop shadow to a widget."""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(20)
    shadow.setOffset(0, 2)
    shadow.setColor(QColor(0, 0, 0, 16))
    widget.setGraphicsEffect(shadow)


# ---------------------------------------------------------------------------
# Global stylesheet
# ---------------------------------------------------------------------------

_GLOBAL_QSS = f"""
QWidget#CortexDashboard {{
    background-color: {CX_BG};
}}
QTabWidget::pane {{
    border: none;
    background: {CX_BG};
}}
QTabBar {{
    background: {CX_BG};
    border: none;
}}
QTabBar::tab {{
    background: transparent;
    color: {CX_TEXT_TERTIARY};
    font-family: {CX_FONT_SANS};
    font-size: 13px;
    font-weight: 500;
    padding: 12px 24px 10px 24px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: 0px;
}}
QTabBar::tab:selected {{
    color: {CX_TEXT};
    font-weight: 600;
    border-bottom: 2px solid {CX_ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {CX_TEXT_SECONDARY};
}}
"""


# ---------------------------------------------------------------------------
# Tab 1: Consumer Dashboard
# ---------------------------------------------------------------------------

class _ConsumerTab(QWidget):
    """Clean biometrics dashboard matching the browser extension popup."""

    # E.1: surface user intent for the daemon orchestrator. The shell
    # only owns the widgets; the parent dashboard re-emits these signals
    # so the desktop app (in-process or WebSocket mode) can route them
    # to ``RuntimeDaemon._handle_user_action`` (goal_set) and to
    # ``_shutdown_daemon`` (stop_requested).
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {CX_BG};")

        root = QVBoxLayout(self)
        root.setContentsMargins(SP6, SP5, SP6, SP6)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, SP5)
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {CX_FONT_BRAND}; "
            f"font-style: italic; font-size: 20px; font-weight: 400; "
            f"color: {CX_TEXT}; background: transparent;"
        )
        header.addWidget(brand)
        header.addStretch()

        # State pill badge
        self._state_badge = QWidget()
        badge_layout = QHBoxLayout(self._state_badge)
        badge_layout.setContentsMargins(10, 4, 12, 4)
        badge_layout.setSpacing(6)

        self._state_dot = QLabel()
        self._state_dot.setFixedSize(7, 7)
        self._state_dot.setStyleSheet(
            f"background: {CX_TEXT_TERTIARY}; border-radius: 3px;"
        )
        badge_layout.addWidget(self._state_dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_label = QLabel("Disconnected")
        self._state_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; font-weight: 500; "
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        badge_layout.addWidget(self._state_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_badge.setStyleSheet(
            f"background: {CX_TERTIARY}; border-radius: {RADIUS_FULL}px;"
        )
        header.addWidget(self._state_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        # ── Goal input ────────────────────────────────────────────────
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setFixedHeight(42)
        self._goal_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 0 {SP4}px;
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_SM}px;
                font-family: {CX_FONT_SANS};
                font-size: 13px;
                color: {CX_TEXT};
                background: {CX_SURFACE};
            }}
            QLineEdit:focus {{
                border: 1.5px solid {CX_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {CX_TEXT_TERTIARY};
            }}
        """)
        # E.1: emit goal_set when the user hits return (the daemon
        # forwards it through USER_ACTION{action: "set_goal"}).
        self._goal_input.returnPressed.connect(
            lambda: self.goal_set.emit(self._goal_input.text().strip())
        )
        root.addWidget(self._goal_input)
        root.addSpacing(SP5)

        # ── Biometrics card ───────────────────────────────────────────
        bio_card = QFrame()
        bio_card.setStyleSheet(f"""
            QFrame {{
                {CARD_QSS}
            }}
        """)
        _card_shadow(bio_card)
        bio_inner = QVBoxLayout(bio_card)
        bio_inner.setContentsMargins(SP5, SP4, SP5, SP4)
        bio_inner.setSpacing(SP3)

        # Bio heading
        bio_heading = QLabel("BIOMETRICS")
        bio_heading.setStyleSheet(SECTION_HEADING_QSS)
        bio_inner.addWidget(bio_heading)

        # Bio values row
        bio_row = QHBoxLayout()
        bio_row.setSpacing(0)

        self._bpm_label = QLabel("--")
        self._hrv_label = QLabel("--")
        self._blk_label = QLabel("--")

        for val_widget, title, color in [
            (self._bpm_label, "BPM", CX_BIO_HR),
            (self._hrv_label, "HRV", CX_BIO_HRV),
            (self._blk_label, "BLK", CX_BIO_BLINK),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setStyleSheet(
                f"font-family: {CX_FONT_BRAND}; font-size: 32px; "
                f"font-weight: 400; color: {CX_TEXT}; "
                f"background: transparent; border: none;"
            )

            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 10px; font-weight: 600; "
                f"letter-spacing: 1px; color: {color}; "
                f"background: transparent; border: none;"
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
                f"background: {CX_TEXT_TERTIARY}; border-radius: 3px;"
            )
            lbl = QLabel(name)
            lbl.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 11px; "
                f"color: {CX_TEXT_TERTIARY}; background: transparent;"
            )
            conn_row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)
            conn_row.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
            self._conn_dots[name] = dot

        conn_row.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._connect_btn.setStyleSheet(f"""
            QPushButton {{
                font-family: {CX_FONT_SANS};
                font-size: 12px; font-weight: 600;
                color: {CX_ACCENT}; background: transparent;
                border: none; padding: 4px 0;
            }}
            QPushButton:hover {{ color: {CX_TEXT}; }}
        """)
        conn_row.addWidget(self._connect_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(conn_row)
        root.addSpacing(SP5)

        # ── Divider ───────────────────────────────────────────────────
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {CX_BORDER};")
        root.addWidget(divider)
        root.addSpacing(SP5)

        # ── Today stats ──────────────────────────────────────────────
        today_label = QLabel("TODAY")
        today_label.setStyleSheet(SECTION_HEADING_QSS)
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
                f"font-family: {CX_FONT_BRAND}; font-size: 20px; "
                f"color: {CX_TEXT}; background: transparent;"
            )
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 10px; font-weight: 500; "
                f"letter-spacing: 0.5px; color: {CX_TEXT_TERTIARY}; "
                f"background: transparent;"
            )
            col.addWidget(val_widget)
            col.addWidget(heading)
            today_row.addLayout(col, stretch=1)

        root.addLayout(today_row)
        root.addStretch()

        # ── Stop button ──────────────────────────────────────────────
        root.addSpacing(SP4)
        self._stop_btn = QPushButton("Stop Cortex")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setFixedHeight(38)
        self._stop_btn.setStyleSheet(f"""
            QPushButton {{
                border: 1px solid rgba(217, 87, 87, 0.12);
                background: rgba(217, 87, 87, 0.04);
                color: {CX_DANGER};
                font-family: {CX_FONT_SANS};
                font-size: 12px; font-weight: 500;
                border-radius: {RADIUS_SM}px;
            }}
            QPushButton:hover {{
                background: rgba(217, 87, 87, 0.10);
            }}
        """)
        # E.1: emit stop_requested so the parent dashboard can re-emit
        # and the app-level handler can call _shutdown_daemon. Previously
        # the button had no handler — clicking it did nothing.
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        root.addWidget(self._stop_btn)

    # -- Public update methods ------------------------------------------------

    def update_state(self, payload: dict) -> None:
        state = payload.get("state", "FLOW")
        color = STATE_COLORS.get(state, CX_TEXT_TERTIARY)
        label = STATE_LABELS.get(state, state)
        self._state_dot.setStyleSheet(
            f"background: {color}; border-radius: 3px;"
        )
        self._state_label.setText(label)
        self._state_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; font-weight: 500; "
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
                f"background: {CX_ACCENT}; border-radius: 3px;"
            )
        else:
            self._state_label.setText("Disconnected")
            self._state_dot.setStyleSheet(
                f"background: {CX_TEXT_TERTIARY}; border-radius: 3px;"
            )


# ---------------------------------------------------------------------------
# HR Trace Plot (warm palette)
# ---------------------------------------------------------------------------

class HRTracePlot(QWidget):
    """Rolling 60s HR trace with warm styling."""

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

        # Background — rounded rect
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(CX_SURFACE))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS_MD, RADIUS_MD)
        painter.drawPath(path)

        # Border
        painter.setPen(QPen(QColor(0, 0, 0, 12), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        if len(self._values) < 2:
            painter.setPen(QColor(CX_TEXT_TERTIARY))
            f = QFont("Georgia", 12)
            painter.setFont(f)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for HR data...")
            painter.end()
            return

        min_hr = max(40.0, min(self._values) - 5)
        max_hr = min(180.0, max(self._values) + 5)
        hr_range = max(max_hr - min_hr, 10.0)

        # Subtle grid lines
        painter.setPen(QPen(QColor(0, 0, 0, 6), 1))
        for tick in range(int(min_hr), int(max_hr) + 1, 10):
            y = pad + (h - 2 * pad) - int((tick - min_hr) / hr_range * (h - 2 * pad))
            painter.drawLine(pad, y, w - pad, y)

        # Trace line — smooth, terracotta
        pen = QPen(QColor(CX_BIO_HR), 2)
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

        # Current value — bottom right
        painter.setPen(QColor(CX_TEXT))
        f = QFont("Georgia", 12)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(w - 80, h - 12, f"{vals[-1]:.0f} BPM")

        painter.end()


# ---------------------------------------------------------------------------
# Signal quality bar (refined)
# ---------------------------------------------------------------------------

class _SignalQualityBar(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self._label = QLabel(label)
        self._label.setFixedWidth(76)
        self._label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background: {CX_TERTIARY};
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background: {CX_ACCENT};
                border-radius: 2px;
            }}
        """)
        layout.addWidget(self._bar)

        self._val_label = QLabel("0%")
        self._val_label.setFixedWidth(36)
        self._val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._val_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; "
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        layout.addWidget(self._val_label)

    def set_value(self, quality: float) -> None:
        pct = int(quality * 100)
        self._bar.setValue(pct)
        self._val_label.setText(f"{pct}%")
        if quality >= 0.7:
            color = "#4CAF7D"
        elif quality >= 0.4:
            color = "#D9B457"
        else:
            color = CX_DANGER
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background: {CX_TERTIARY};
                border: none; border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background: {color};
                border-radius: 2px;
            }}
        """)


# ---------------------------------------------------------------------------
# Tab 2: Advanced
# ---------------------------------------------------------------------------

class _AdvancedTab(QWidget):
    """Developer debug view with refined warm styling."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {CX_BG};")
        self._timeline_events: list[dict] = []
        self._session_start = time.monotonic()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP4)

        # ── Signal quality ────────────────────────────────────────────
        sq_label = QLabel("SIGNAL QUALITY")
        sq_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(sq_label)

        self._physio_q = _SignalQualityBar("Physio")
        self._kine_q = _SignalQualityBar("Kinematics")
        self._tele_q = _SignalQualityBar("Telemetry")
        layout.addWidget(self._physio_q)
        layout.addWidget(self._kine_q)
        layout.addWidget(self._tele_q)
        layout.addSpacing(SP2)

        # ── HR trace ──────────────────────────────────────────────────
        hr_label = QLabel("HEART RATE")
        hr_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(hr_label)
        self._hr_plot = HRTracePlot()
        layout.addWidget(self._hr_plot)

        # ── State scores ─────────────────────────────────────────────
        scores_label = QLabel("STATE SCORES")
        scores_label.setStyleSheet(SECTION_HEADING_QSS)
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
            lbl.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 12px; "
                f"color: {CX_TEXT_SECONDARY}; background: transparent;"
            )
            scores_grid.addWidget(lbl, i, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(5)
            bar.setTextVisible(False)
            bar.setStyleSheet(f"""
                QProgressBar {{ background: {CX_TERTIARY}; border: none; border-radius: 2px; }}
                QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}
            """)
            scores_grid.addWidget(bar, i, 1)
            val_lbl = QLabel("0.00")
            val_lbl.setFixedWidth(36)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_lbl.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 11px; "
                f"color: {CX_TEXT_TERTIARY}; background: transparent;"
            )
            scores_grid.addWidget(val_lbl, i, 2)
            self._score_bars[name] = bar
            self._score_labels[name] = val_lbl
        layout.addLayout(scores_grid)

        # ── Confidence / dwell ────────────────────────────────────────
        meta_row = QHBoxLayout()
        self._confidence_lbl = QLabel("Confidence: --")
        self._confidence_lbl.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        self._dwell_lbl = QLabel("Dwell: --")
        self._dwell_lbl.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        meta_row.addWidget(self._confidence_lbl)
        meta_row.addStretch()
        meta_row.addWidget(self._dwell_lbl)
        layout.addLayout(meta_row)

        # ── Timeline ──────────────────────────────────────────────────
        tl_label = QLabel("TIMELINE")
        tl_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(tl_label)
        self._timeline_text = QLabel("No events yet")
        self._timeline_text.setWordWrap(True)
        self._timeline_text.setStyleSheet(
            f"font-family: {CX_FONT_MONO}; "
            f"font-size: 11px; color: {CX_TEXT_SECONDARY}; "
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

        # Timeline
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
    """Two-tab dashboard: consumer view + advanced debug view."""

    # E.1: re-emit user-intent signals from the consumer tab so the
    # app-level orchestrator can wire them once at construction time
    # without reaching into widget internals.
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Backward-compatible test-facing fields.
        self._connected = False
        self.setObjectName("CortexDashboard")
        self.setWindowTitle("Cortex")
        self.setFixedWidth(DASHBOARD_WIDTH)
        self.setMaximumHeight(DASHBOARD_MAX_HEIGHT)
        self.setStyleSheet(_GLOBAL_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._consumer = _ConsumerTab()
        self._advanced = _AdvancedTab()
        self._timeline_events = self._advanced._timeline_events
        self._tabs.addTab(self._consumer, "Dashboard")
        self._tabs.addTab(self._advanced, "Advanced")
        layout.addWidget(self._tabs)

        # E.1: forward consumer-tab signals to outer subscribers.
        self._consumer.stop_requested.connect(self.stop_requested.emit)
        self._consumer.goal_set.connect(self.goal_set.emit)

    def update_state(self, payload: dict) -> None:
        self._consumer.update_state(payload)
        self._advanced.update_state(payload)

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._consumer.set_connected(connected)
