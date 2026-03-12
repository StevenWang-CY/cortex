"""
Desktop Shell — Settings Dialog

Settings panel for Cortex configuration:
- Webcam toggle (enable/disable sensing)
- Intervention toggle (enable/disable auto-interventions)
- Sensitivity slider (1-5 scale, maps to trigger thresholds)
- Cooldown duration (seconds)
- Quiet mode toggle and duration
- LLM backend selector (remote/local)
- Debug toggles
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class SettingsDialog(QWidget):
    """
    Settings dialog for Cortex desktop shell.

    Emits settings_changed(dict) when the user applies changes.
    """

    settings_changed = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Settings")
        self.setMinimumSize(400, 500)

        self._build_ui()

    def _build_ui(self) -> None:
        """Build the settings UI."""
        layout = QVBoxLayout(self)

        # --- Sensing ---
        sensing_group = QGroupBox("Sensing")
        sensing_layout = QFormLayout(sensing_group)

        self._webcam_enabled = QCheckBox("Enable webcam")
        self._webcam_enabled.setChecked(True)
        sensing_layout.addRow(self._webcam_enabled)

        self._input_telemetry_enabled = QCheckBox("Enable keyboard/mouse tracking")
        self._input_telemetry_enabled.setChecked(True)
        sensing_layout.addRow(self._input_telemetry_enabled)

        layout.addWidget(sensing_group)

        # --- Intervention ---
        intervention_group = QGroupBox("Interventions")
        intervention_layout = QFormLayout(intervention_group)

        self._interventions_enabled = QCheckBox("Enable auto-interventions")
        self._interventions_enabled.setChecked(True)
        intervention_layout.addRow(self._interventions_enabled)

        # Sensitivity slider (1-5)
        sensitivity_row = QHBoxLayout()
        self._sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self._sensitivity_slider.setRange(1, 5)
        self._sensitivity_slider.setValue(3)
        self._sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._sensitivity_slider.setTickInterval(1)
        sensitivity_row.addWidget(self._sensitivity_slider)

        self._sensitivity_label = QLabel("3")
        self._sensitivity_label.setFixedWidth(20)
        self._sensitivity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sensitivity_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        sensitivity_row.addWidget(self._sensitivity_label)

        self._sensitivity_slider.valueChanged.connect(
            lambda v: self._sensitivity_label.setText(str(v))
        )
        intervention_layout.addRow("Sensitivity (1-5):", sensitivity_row)

        # Sensitivity description
        sens_desc = QLabel(
            "1 = Less sensitive (fewer interventions)\n"
            "5 = More sensitive (intervene earlier)"
        )
        sens_desc.setStyleSheet("color: #888; font-size: 11px;")
        intervention_layout.addRow(sens_desc)

        # Cooldown
        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(10, 600)
        self._cooldown_spin.setValue(60)
        self._cooldown_spin.setSuffix(" sec")
        intervention_layout.addRow("Cooldown:", self._cooldown_spin)

        # Quiet mode
        self._quiet_mode = QCheckBox("Quiet mode (suppress all interventions)")
        self._quiet_mode.setChecked(False)
        intervention_layout.addRow(self._quiet_mode)

        self._quiet_duration = QSpinBox()
        self._quiet_duration.setRange(5, 120)
        self._quiet_duration.setValue(30)
        self._quiet_duration.setSuffix(" min")
        intervention_layout.addRow("Quiet duration:", self._quiet_duration)

        layout.addWidget(intervention_group)

        # --- LLM Backend ---
        llm_group = QGroupBox("LLM Backend")
        llm_layout = QFormLayout(llm_group)

        self._llm_backend = QComboBox()
        self._llm_backend.addItems([
            "Remote (Qwen-3-8B on gwhiz1)",
            "Local (Ollama)",
            "Rule-based (no LLM)",
        ])
        llm_layout.addRow("Backend:", self._llm_backend)

        layout.addWidget(llm_group)

        # --- Debug ---
        debug_group = QGroupBox("Debug")
        debug_layout = QFormLayout(debug_group)

        self._debug_capture = QCheckBox("Capture debug")
        self._debug_rppg = QCheckBox("rPPG debug")
        self._debug_state = QCheckBox("State debug")
        self._debug_llm = QCheckBox("LLM debug")

        debug_layout.addRow(self._debug_capture)
        debug_layout.addRow(self._debug_rppg)
        debug_layout.addRow(self._debug_state)
        debug_layout.addRow(self._debug_llm)

        layout.addWidget(debug_group)

        # --- Buttons ---
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply_settings
        )
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.hide
        )
        layout.addWidget(buttons)

    def get_settings(self) -> dict:
        """Get current settings as a dict."""
        # Map sensitivity (1-5) to threshold adjustments
        sensitivity = self._sensitivity_slider.value()
        # Sensitivity 1 = threshold 0.95, 3 = 0.85, 5 = 0.75
        threshold_offset = (3 - sensitivity) * 0.05

        # Map LLM backend combo to mode string
        llm_modes = ["remote", "local", "rule_based"]
        llm_mode = llm_modes[self._llm_backend.currentIndex()]

        return {
            "webcam_enabled": self._webcam_enabled.isChecked(),
            "input_telemetry_enabled": self._input_telemetry_enabled.isChecked(),
            "interventions_enabled": self._interventions_enabled.isChecked(),
            "sensitivity": sensitivity,
            "entry_threshold": 0.85 + threshold_offset,
            "cooldown_seconds": self._cooldown_spin.value(),
            "quiet_mode": self._quiet_mode.isChecked(),
            "quiet_duration_minutes": self._quiet_duration.value(),
            "llm_mode": llm_mode,
            "debug_capture": self._debug_capture.isChecked(),
            "debug_rppg": self._debug_rppg.isChecked(),
            "debug_state": self._debug_state.isChecked(),
            "debug_llm": self._debug_llm.isChecked(),
        }

    def _apply_settings(self) -> None:
        """Apply and emit current settings."""
        settings = self.get_settings()
        self.settings_changed.emit(settings)
        logger.info(f"Settings applied: sensitivity={settings['sensitivity']}, "
                     f"llm={settings['llm_mode']}")
