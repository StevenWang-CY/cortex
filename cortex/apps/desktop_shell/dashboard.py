"""
Desktop Shell — Dashboard Window

Live monitoring dashboard showing:
- Current state indicator with confidence bar
- Signal quality meters (physio, kinematics, telemetry)
- HR trace plot (rolling 60s)
- Session timeline of state transitions
- Connection status
"""

from __future__ import annotations

import collections
import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# State color map
_STATE_COLORS = {
    "FLOW": "#4CAF50",
    "HYPER": "#F44336",
    "HYPO": "#6495ED",
    "RECOVERY": "#FFC107",
}

_MAX_HR_HISTORY = 120  # 60 seconds at 2 updates/sec
_MAX_TIMELINE_EVENTS = 50


class HRTracePlot(QWidget):
    """Simple HR trace plot widget (rolling 60s)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: collections.deque[float] = collections.deque(maxlen=_MAX_HR_HISTORY)
        self.setMinimumHeight(100)
        self.setMinimumWidth(300)

    def add_value(self, hr: float) -> None:
        """Add a heart rate value to the trace."""
        self._values.append(hr)
        self.update()

    def paintEvent(self, event: object) -> None:
        """Paint the HR trace."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Background
        painter.fillRect(0, 0, w, h, QColor(30, 30, 40))

        if len(self._values) < 2:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for HR data...")
            painter.end()
            return

        # Compute Y range
        min_hr = max(40.0, min(self._values) - 5)
        max_hr = min(180.0, max(self._values) + 5)
        hr_range = max_hr - min_hr
        if hr_range < 10:
            hr_range = 10
            min_hr = (min_hr + max_hr) / 2 - 5
            max_hr = min_hr + 10

        # Draw grid lines
        painter.setPen(QPen(QColor(60, 60, 70), 1))
        for hr_tick in range(int(min_hr), int(max_hr) + 1, 10):
            y = h - int((hr_tick - min_hr) / hr_range * h)
            painter.drawLine(0, y, w, y)
            painter.drawText(2, y - 2, f"{hr_tick}")

        # Draw trace
        painter.setPen(QPen(QColor(76, 175, 80), 2))
        n = len(self._values)
        values_list = list(self._values)
        for i in range(1, n):
            x1 = int((i - 1) / max(n - 1, 1) * w)
            x2 = int(i / max(n - 1, 1) * w)
            y1 = h - int((values_list[i - 1] - min_hr) / hr_range * h)
            y2 = h - int((values_list[i] - min_hr) / hr_range * h)
            painter.drawLine(x1, y1, x2, y2)

        # Current value label
        current = values_list[-1]
        painter.setPen(QColor(255, 255, 255))
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(w - 70, 20, f"{current:.0f} BPM")

        painter.end()


class SignalQualityBar(QWidget):
    """Compact signal quality indicator."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(label)
        self._label.setFixedWidth(80)
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%v%")
        self._bar.setFixedHeight(18)
        layout.addWidget(self._bar)

    def set_value(self, quality: float) -> None:
        """Set quality value (0.0-1.0)."""
        pct = int(quality * 100)
        self._bar.setValue(pct)

        # Color by quality level
        if quality >= 0.7:
            color = "#4CAF50"
        elif quality >= 0.4:
            color = "#FFC107"
        else:
            color = "#F44336"

        self._bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
        )


class DashboardWindow(QWidget):
    """
    Main dashboard window for live Cortex monitoring.

    Displays:
    - State indicator with confidence
    - Signal quality meters
    - HR trace plot
    - Session timeline
    - Connection status
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Dashboard")
        self.setMinimumSize(500, 600)

        self._connected = False
        self._timeline_events: list[dict] = []
        self._session_start = time.monotonic()

        self._build_ui()

    def _build_ui(self) -> None:
        """Build the dashboard UI layout."""
        layout = QVBoxLayout(self)

        # --- Connection status ---
        self._connection_label = QLabel("Disconnected")
        self._connection_label.setStyleSheet(
            "color: #999; font-size: 12px; padding: 2px;"
        )
        layout.addWidget(self._connection_label)

        # --- State indicator ---
        state_group = QGroupBox("Current State")
        state_layout = QHBoxLayout(state_group)

        self._state_label = QLabel("—")
        self._state_label.setFont(QFont("Arial", 28, QFont.Weight.Bold))
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        state_layout.addWidget(self._state_label)

        confidence_layout = QVBoxLayout()
        confidence_layout.addWidget(QLabel("Confidence"))
        self._confidence_bar = QProgressBar()
        self._confidence_bar.setRange(0, 100)
        self._confidence_bar.setValue(0)
        self._confidence_bar.setFormat("%v%")
        confidence_layout.addWidget(self._confidence_bar)

        self._dwell_label = QLabel("Dwell: 0.0s")
        confidence_layout.addWidget(self._dwell_label)

        self._reasons_label = QLabel("")
        self._reasons_label.setWordWrap(True)
        self._reasons_label.setStyleSheet("color: #888; font-size: 11px;")
        confidence_layout.addWidget(self._reasons_label)

        state_layout.addLayout(confidence_layout)
        layout.addWidget(state_group)

        # --- Signal quality ---
        quality_group = QGroupBox("Signal Quality")
        quality_layout = QVBoxLayout(quality_group)

        self._physio_quality = SignalQualityBar("Physio")
        self._kinematics_quality = SignalQualityBar("Kinematics")
        self._telemetry_quality = SignalQualityBar("Telemetry")

        quality_layout.addWidget(self._physio_quality)
        quality_layout.addWidget(self._kinematics_quality)
        quality_layout.addWidget(self._telemetry_quality)
        layout.addWidget(quality_group)

        # --- HR trace ---
        hr_group = QGroupBox("Heart Rate Trace (60s)")
        hr_layout = QVBoxLayout(hr_group)
        self._hr_plot = HRTracePlot()
        hr_layout.addWidget(self._hr_plot)
        layout.addWidget(hr_group)

        # --- State scores ---
        scores_group = QGroupBox("State Scores")
        scores_layout = QGridLayout(scores_group)

        self._score_labels: dict[str, QLabel] = {}
        self._score_bars: dict[str, QProgressBar] = {}

        for i, name in enumerate(["flow", "hypo", "hyper", "recovery"]):
            label = QLabel(name.upper())
            label.setFixedWidth(80)
            scores_layout.addWidget(label, i, 0)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("%v%")
            bar.setFixedHeight(16)
            scores_layout.addWidget(bar, i, 1)

            value_label = QLabel("0.00")
            value_label.setFixedWidth(40)
            scores_layout.addWidget(value_label, i, 2)

            self._score_labels[name] = value_label
            self._score_bars[name] = bar

        layout.addWidget(scores_group)

        # --- Session timeline ---
        timeline_group = QGroupBox("Session Timeline")
        timeline_layout = QVBoxLayout(timeline_group)
        self._timeline_label = QLabel("No events yet")
        self._timeline_label.setWordWrap(True)
        self._timeline_label.setStyleSheet("font-family: monospace; font-size: 11px;")
        self._timeline_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        timeline_layout.addWidget(self._timeline_label)
        layout.addWidget(timeline_group)

    def update_state(self, payload: dict) -> None:
        """Update dashboard with a STATE_UPDATE payload."""
        state = payload.get("state", "—")
        confidence = payload.get("confidence", 0.0)
        scores = payload.get("scores", {})
        signal_quality = payload.get("signal_quality", {})
        dwell = payload.get("dwell_seconds", 0.0)
        reasons = payload.get("reasons", [])

        # State label with color
        color = _STATE_COLORS.get(state, "#999")
        self._state_label.setText(state)
        self._state_label.setStyleSheet(f"color: {color};")

        # Confidence
        self._confidence_bar.setValue(int(confidence * 100))

        # Dwell
        self._dwell_label.setText(f"Dwell: {dwell:.1f}s")

        # Reasons
        self._reasons_label.setText(", ".join(reasons) if reasons else "")

        # Signal quality
        self._physio_quality.set_value(signal_quality.get("physio", 0.0))
        self._kinematics_quality.set_value(signal_quality.get("kinematics", 0.0))
        self._telemetry_quality.set_value(signal_quality.get("telemetry", 0.0))

        # State scores
        score_colors = {
            "flow": "#4CAF50",
            "hypo": "#6495ED",
            "hyper": "#F44336",
            "recovery": "#FFC107",
        }
        for name in ["flow", "hypo", "hyper", "recovery"]:
            val = scores.get(name, 0.0)
            self._score_bars[name].setValue(int(val * 100))
            self._score_labels[name].setText(f"{val:.2f}")
            sc = score_colors.get(name, "#999")
            self._score_bars[name].setStyleSheet(
                f"QProgressBar::chunk {{ background-color: {sc}; }}"
            )

        # Track state transitions for timeline
        self._record_timeline_event(state, confidence)

    def set_connected(self, connected: bool) -> None:
        """Update connection status display."""
        self._connected = connected
        if connected:
            self._connection_label.setText("Connected to Cortex daemon")
            self._connection_label.setStyleSheet(
                "color: #4CAF50; font-size: 12px; padding: 2px;"
            )
        else:
            self._connection_label.setText("Disconnected")
            self._connection_label.setStyleSheet(
                "color: #F44336; font-size: 12px; padding: 2px;"
            )

    def _record_timeline_event(self, state: str, confidence: float) -> None:
        """Record a state observation for timeline display."""
        elapsed = time.monotonic() - self._session_start

        # Only record if state changed
        if (
            self._timeline_events
            and self._timeline_events[-1]["state"] == state
        ):
            return

        event = {
            "time": elapsed,
            "state": state,
            "confidence": confidence,
        }
        self._timeline_events.append(event)

        # Keep last N events
        if len(self._timeline_events) > _MAX_TIMELINE_EVENTS:
            self._timeline_events = self._timeline_events[-_MAX_TIMELINE_EVENTS:]

        # Update display
        lines = []
        for ev in reversed(self._timeline_events[-10:]):
            t = ev["time"]
            minutes = int(t // 60)
            seconds = t % 60
            color = _STATE_COLORS.get(ev["state"], "#999")
            lines.append(
                f"{minutes:02d}:{seconds:05.2f}  {ev['state']:<8}  "
                f"({ev['confidence']:.0%})"
            )
        self._timeline_label.setText("\n".join(lines) if lines else "No events yet")
