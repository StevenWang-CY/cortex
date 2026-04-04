"""
Desktop Shell — Dashboard Window

Two-tab layout:
  Tab 1 "Dashboard" — Consumer biometrics view matching the browser extension popup
  Tab 2 "Advanced"  — Developer debug view with HR trace, signal quality, state scores
"""

from __future__ import annotations

import collections
import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell.tokens import (
    CX_ACCENT,
    CX_BG,
    CX_BIO_BLINK,
    CX_BIO_HR,
    CX_BIO_HRV,
    CX_BORDER_DEFAULT,
    CX_DANGER,
    CX_DANGER_DIM,
    CX_SURFACE,
    CX_TERTIARY,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DASHBOARD_MAX_HEIGHT,
    DASHBOARD_WIDTH,
    HEADER_HEIGHT,
    RADIUS_FULL,
    RADIUS_MD,
    SP4,
    SP5,
    STATE_COLORS,
    STATE_LABELS,
)

logger = logging.getLogger(__name__)

_MAX_HR_HISTORY = 120
_MAX_TIMELINE_EVENTS = 50

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
}}
QTabBar::tab {{
    background: transparent;
    color: {CX_TEXT_TERTIARY};
    font-size: 13px;
    font-weight: 500;
    padding: 8px 16px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {CX_TEXT};
    border-bottom: 2px solid {CX_ACCENT};
}}
QTabBar::tab:hover {{
    color: {CX_TEXT_SECONDARY};
}}
"""


# ---------------------------------------------------------------------------
# Reusable card frame
# ---------------------------------------------------------------------------

class _Card(QFrame):
    """White card with shadow and rounded corners."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            _Card {{
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_MD}px;
            }}
        """)


# ---------------------------------------------------------------------------
# Tab 1: Consumer Dashboard (matches browser extension popup)
# ---------------------------------------------------------------------------

class _ConsumerTab(QWidget):
    """Clean biometrics dashboard mirroring the browser extension popup."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {CX_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP4)

        # -- Header -----------------------------------------------------------
        header = QHBoxLayout()
        brand = QLabel("Cortex.")
        brand.setStyleSheet(
            f"font-family: Georgia, serif; font-style: italic; "
            f"font-size: 20px; color: {CX_TEXT};"
        )
        header.addWidget(brand)
        header.addStretch()

        self._state_dot = QLabel("\u2B24")  # Black circle
        self._state_dot.setStyleSheet(f"font-size: 10px; color: {CX_TEXT_TERTIARY};")
        header.addWidget(self._state_dot)

        self._state_label = QLabel("Disconnected")
        self._state_label.setStyleSheet(
            f"font-size: 13px; color: {CX_TEXT_SECONDARY}; margin-left: 4px;"
        )
        header.addWidget(self._state_label)
        layout.addLayout(header)

        # -- Goal input -------------------------------------------------------
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setStyleSheet(f"""
            QLineEdit {{
                height: 44px;
                padding: 0 16px;
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_FULL}px;
                font-size: 14px;
                color: {CX_TEXT};
                background: {CX_SURFACE};
            }}
            QLineEdit:focus {{
                border: 2px solid {CX_ACCENT};
            }}
        """)
        layout.addWidget(self._goal_input)

        # -- Biometrics row ---------------------------------------------------
        bio_row = QHBoxLayout()
        bio_row.setSpacing(0)

        self._bpm_label = QLabel("--")
        self._hrv_label = QLabel("--")
        self._blk_label = QLabel("--")

        for label_widget, title, color in [
            (self._bpm_label, "BPM", CX_BIO_HR),
            (self._hrv_label, "HRV", CX_BIO_HRV),
            (self._blk_label, "BLK", CX_BIO_BLINK),
        ]:
            col = QVBoxLayout()
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading = QLabel(title)
            heading.setStyleSheet(
                f"font-size: 11px; font-weight: 500; "
                f"text-transform: uppercase; color: {color};"
            )
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label_widget.setStyleSheet(
                f"font-family: Georgia, serif; font-size: 18px; color: {CX_TEXT};"
            )
            label_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(heading)
            col.addWidget(label_widget)
            bio_row.addLayout(col)

        bio_frame = QFrame()
        bio_frame.setLayout(bio_row)
        bio_frame.setStyleSheet(f"""
            QFrame {{
                border-top: 1px solid {CX_BORDER_DEFAULT};
                border-bottom: 1px solid {CX_BORDER_DEFAULT};
                padding: {SP4}px {SP4 // 2}px;
            }}
        """)
        layout.addWidget(bio_frame)

        # -- Connections bar --------------------------------------------------
        conn_row = QHBoxLayout()
        conn_row.setSpacing(SP4)

        self._chrome_dot = QLabel("\u2B24")
        self._chrome_dot.setStyleSheet(f"font-size: 8px; color: {CX_TEXT_TERTIARY};")
        self._chrome_lbl = QLabel("Chrome")
        self._chrome_lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")

        self._edge_dot = QLabel("\u2B24")
        self._edge_dot.setStyleSheet(f"font-size: 8px; color: {CX_TEXT_TERTIARY};")
        self._edge_lbl = QLabel("Edge")
        self._edge_lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")

        self._vscode_dot = QLabel("\u2B24")
        self._vscode_dot.setStyleSheet(f"font-size: 8px; color: {CX_TEXT_TERTIARY};")
        self._vscode_lbl = QLabel("Editor")
        self._vscode_lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")

        for dot, lbl in [
            (self._chrome_dot, self._chrome_lbl),
            (self._edge_dot, self._edge_lbl),
            (self._vscode_dot, self._vscode_lbl),
        ]:
            conn_row.addWidget(dot)
            conn_row.addWidget(lbl)

        conn_row.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setStyleSheet(f"""
            QPushButton {{
                font-size: 12px; font-weight: 500;
                color: {CX_ACCENT}; background: transparent;
                border: none; text-decoration: underline;
                padding: 4px 8px;
            }}
            QPushButton:hover {{ color: {CX_TEXT}; }}
        """)
        conn_row.addWidget(self._connect_btn)
        layout.addLayout(conn_row)

        # -- Stop button ------------------------------------------------------
        self._stop_btn = QPushButton("Stop Cortex")
        self._stop_btn.setStyleSheet(f"""
            QPushButton {{
                width: 100%; padding: 10px 0;
                border: 1px solid rgba(217, 87, 87, 0.2);
                background: {CX_DANGER_DIM};
                color: {CX_DANGER};
                font-size: 12px; font-weight: 500;
                border-radius: {RADIUS_MD}px;
            }}
            QPushButton:hover {{ background: rgba(217, 87, 87, 0.15); }}
        """)
        layout.addWidget(self._stop_btn)

        # -- Today footer -----------------------------------------------------
        today_row = QHBoxLayout()
        today_row.setSpacing(0)

        self._today_focus = QLabel("--")
        self._today_sessions = QLabel("--")
        self._today_best = QLabel("--")
        self._today_blocked = QLabel("--")

        for val_widget, title in [
            (self._today_focus, "FOCUS"),
            (self._today_sessions, "SESSIONS"),
            (self._today_best, "BEST"),
            (self._today_blocked, "BLOCKED"),
        ]:
            col = QVBoxLayout()
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setStyleSheet(
                f"font-family: Georgia, serif; font-size: 16px; color: {CX_TEXT};"
            )
            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading = QLabel(title)
            heading.setStyleSheet(
                f"font-size: 11px; font-weight: 500; color: {CX_TEXT_TERTIARY};"
            )
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(val_widget)
            col.addWidget(heading)
            today_row.addLayout(col)

        today_frame = QFrame()
        today_frame.setLayout(today_row)
        today_frame.setStyleSheet(f"""
            QFrame {{
                border-top: 1px solid {CX_BORDER_DEFAULT};
                padding: {SP5}px {SP4 // 2}px 0 {SP4 // 2}px;
                margin-top: {SP4}px;
            }}
        """)
        layout.addWidget(today_frame)
        layout.addStretch()

    # -- Public update methods ------------------------------------------------

    def update_state(self, payload: dict) -> None:
        state = payload.get("state", "FLOW")
        color = STATE_COLORS.get(state, CX_TEXT_TERTIARY)
        label = STATE_LABELS.get(state, state)
        self._state_dot.setStyleSheet(f"font-size: 10px; color: {color};")
        self._state_label.setText(label)
        self._state_label.setStyleSheet(f"font-size: 13px; color: {color}; margin-left: 4px;")

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
            self._state_dot.setStyleSheet(f"font-size: 10px; color: {CX_ACCENT};")
        else:
            self._state_label.setText("Disconnected")
            self._state_dot.setStyleSheet(f"font-size: 10px; color: {CX_TEXT_TERTIARY};")


# ---------------------------------------------------------------------------
# HR Trace Plot (reused from original, re-styled)
# ---------------------------------------------------------------------------

class HRTracePlot(QWidget):
    """Rolling 60s HR trace — warm palette."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: collections.deque[float] = collections.deque(maxlen=_MAX_HR_HISTORY)
        self.setMinimumHeight(100)
        self.setMinimumWidth(300)

    def add_value(self, hr: float) -> None:
        self._values.append(hr)
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg = QColor(CX_BG)
        painter.fillRect(0, 0, w, h, bg)

        if len(self._values) < 2:
            painter.setPen(QColor(CX_TEXT_TERTIARY))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for HR data\u2026")
            painter.end()
            return

        min_hr = max(40.0, min(self._values) - 5)
        max_hr = min(180.0, max(self._values) + 5)
        hr_range = max(max_hr - min_hr, 10.0)
        if hr_range < 10:
            min_hr = (min_hr + max_hr) / 2 - 5
            max_hr = min_hr + 10
            hr_range = 10.0

        # Grid
        painter.setPen(QPen(QColor(CX_TERTIARY), 1))
        for tick in range(int(min_hr), int(max_hr) + 1, 10):
            y = h - int((tick - min_hr) / hr_range * h)
            painter.drawLine(0, y, w, y)
            painter.drawText(2, y - 2, f"{tick}")

        # Trace
        painter.setPen(QPen(QColor(CX_BIO_HR), 2))
        vals = list(self._values)
        n = len(vals)
        for i in range(1, n):
            x1 = int((i - 1) / max(n - 1, 1) * w)
            x2 = int(i / max(n - 1, 1) * w)
            y1 = h - int((vals[i - 1] - min_hr) / hr_range * h)
            y2 = h - int((vals[i] - min_hr) / hr_range * h)
            painter.drawLine(x1, y1, x2, y2)

        # Current value
        painter.setPen(QColor(CX_TEXT))
        font = QFont("Georgia", 11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(w - 70, 20, f"{vals[-1]:.0f} BPM")
        painter.end()


# ---------------------------------------------------------------------------
# Signal quality bar (re-styled)
# ---------------------------------------------------------------------------

class _SignalQualityBar(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(label)
        self._label.setFixedWidth(80)
        self._label.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%v%")
        self._bar.setFixedHeight(18)
        layout.addWidget(self._bar)

    def set_value(self, quality: float) -> None:
        pct = int(quality * 100)
        self._bar.setValue(pct)
        if quality >= 0.7:
            color = "#57D99E"
        elif quality >= 0.4:
            color = CX_BIO_BLINK
        else:
            color = CX_DANGER
        self._bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")


# ---------------------------------------------------------------------------
# Tab 2: Advanced (debug / development)
# ---------------------------------------------------------------------------

class _AdvancedTab(QWidget):
    """Developer debug view: HR trace, signal quality, state scores."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {CX_BG};")
        self._timeline_events: list[dict] = []
        self._session_start = time.monotonic()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP4)

        # Signal quality
        sq_label = QLabel("Signal Quality")
        sq_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT};")
        layout.addWidget(sq_label)
        self._physio_q = _SignalQualityBar("Physio")
        self._kine_q = _SignalQualityBar("Kinematics")
        self._tele_q = _SignalQualityBar("Telemetry")
        layout.addWidget(self._physio_q)
        layout.addWidget(self._kine_q)
        layout.addWidget(self._tele_q)

        # HR trace
        hr_label = QLabel("Heart Rate (60s)")
        hr_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT};")
        layout.addWidget(hr_label)
        self._hr_plot = HRTracePlot()
        layout.addWidget(self._hr_plot)

        # State scores
        scores_label = QLabel("State Scores")
        scores_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT};")
        layout.addWidget(scores_label)
        scores_grid = QGridLayout()
        self._score_bars: dict[str, QProgressBar] = {}
        self._score_labels: dict[str, QLabel] = {}
        for i, (name, color) in enumerate([
            ("flow", STATE_COLORS["FLOW"]),
            ("hyper", STATE_COLORS["HYPER"]),
            ("hypo", STATE_COLORS["HYPO"]),
            ("recovery", STATE_COLORS["RECOVERY"]),
        ]):
            lbl = QLabel(name.upper())
            lbl.setFixedWidth(80)
            lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")
            scores_grid.addWidget(lbl, i, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(16)
            bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
            scores_grid.addWidget(bar, i, 1)
            val_lbl = QLabel("0.00")
            val_lbl.setFixedWidth(40)
            val_lbl.setStyleSheet(f"font-size: 11px; color: {CX_TEXT_SECONDARY};")
            scores_grid.addWidget(val_lbl, i, 2)
            self._score_bars[name] = bar
            self._score_labels[name] = val_lbl
        layout.addLayout(scores_grid)

        # Confidence + dwell
        meta_row = QHBoxLayout()
        self._confidence_lbl = QLabel("Confidence: --")
        self._confidence_lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")
        self._dwell_lbl = QLabel("Dwell: --")
        self._dwell_lbl.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_SECONDARY};")
        meta_row.addWidget(self._confidence_lbl)
        meta_row.addWidget(self._dwell_lbl)
        meta_row.addStretch()
        layout.addLayout(meta_row)

        # Timeline
        timeline_label = QLabel("Session Timeline")
        timeline_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT};")
        layout.addWidget(timeline_label)
        self._timeline_text = QLabel("No events yet")
        self._timeline_text.setWordWrap(True)
        self._timeline_text.setStyleSheet(
            f"font-family: 'JetBrains Mono', monospace; font-size: 11px; color: {CX_TEXT_SECONDARY};"
        )
        self._timeline_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._timeline_text)
        layout.addStretch()

    def update_state(self, payload: dict) -> None:
        state = payload.get("state", "FLOW")
        confidence = payload.get("confidence", 0.0)
        scores = payload.get("scores", {})
        sig_q = payload.get("signal_quality", {})
        dwell = payload.get("dwell_seconds", 0.0)
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
            for ev in reversed(self._timeline_events[-10:]):
                t = ev["time"]
                m, s = int(t // 60), t % 60
                lines.append(f"{m:02d}:{s:05.2f}  {ev['state']:<8}  ({ev['confidence']:.0%})")
            self._timeline_text.setText("\n".join(lines) if lines else "No events yet")


# ---------------------------------------------------------------------------
# Main Dashboard Window
# ---------------------------------------------------------------------------

class DashboardWindow(QWidget):
    """Two-tab dashboard: consumer view + advanced debug view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CortexDashboard")
        self.setWindowTitle("Cortex")
        self.setFixedWidth(DASHBOARD_WIDTH)
        self.setMaximumHeight(DASHBOARD_MAX_HEIGHT)
        self.setStyleSheet(_GLOBAL_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._consumer = _ConsumerTab()
        self._advanced = _AdvancedTab()
        self._tabs.addTab(self._consumer, "Dashboard")
        self._tabs.addTab(self._advanced, "Advanced")
        layout.addWidget(self._tabs)

    def update_state(self, payload: dict) -> None:
        """Route state updates to both tabs."""
        self._consumer.update_state(payload)
        self._advanced.update_state(payload)

    def set_connected(self, connected: bool) -> None:
        self._consumer.set_connected(connected)
