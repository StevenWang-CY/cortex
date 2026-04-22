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
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell.tokens import (
    BTN_GHOST_QSS,
    BTN_PRIMARY_QSS,
    CARD_QSS,
    CX_ACCENT,
    CX_BG,
    CX_BORDER,
    CX_BORDER_DEFAULT,
    CX_FONT_SANS,
    CX_SURFACE,
    CX_TERTIARY,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    PAGE_TITLE_QSS,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_SM,
    RADIUS_FULL,
    SECTION_HEADING_QSS,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
    SP8,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared QSS for form controls
# ---------------------------------------------------------------------------

_CHECKBOX_QSS = f"""
    QCheckBox {{
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        color: {CX_TEXT};
        spacing: 8px;
    }}
"""

_COMBO_QSS = f"""
    QComboBox {{
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        color: {CX_TEXT};
        background: {CX_SURFACE};
        border: 1px solid {CX_BORDER_DEFAULT};
        border-radius: {RADIUS_SM}px;
        padding: 8px 12px;
    }}
    QComboBox:hover {{
        border-color: rgba(0,0,0,0.15);
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
"""

_SPINBOX_QSS = f"""
    QSpinBox {{
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        color: {CX_TEXT};
        background: {CX_SURFACE};
        border: 1px solid {CX_BORDER_DEFAULT};
        border-radius: {RADIUS_SM}px;
        padding: 6px 10px;
    }}
"""

_SLIDER_QSS = f"""
    QSlider::groove:horizontal {{
        background: {CX_TERTIARY};
        height: 4px;
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {CX_ACCENT};
        width: 16px;
        height: 16px;
        margin: -6px 0;
        border-radius: 8px;
    }}
    QSlider::handle:horizontal:hover {{
        background: #C46547;
    }}
    QSlider::sub-page:horizontal {{
        background: {CX_ACCENT};
        border-radius: 2px;
    }}
"""


class SettingsDialog(QWidget):
    """
    Settings dialog for Cortex desktop shell.

    Emits settings_changed(dict) when the user applies changes.
    """

    settings_changed = Signal(dict)
    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Settings")
        self.setMinimumSize(420, 540)
        self.setStyleSheet(f"background: {CX_BG};")
        self._build_ui()

    def _build_ui(self) -> None:
        """Build the settings UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP5)

        # ── Header with back button ──────────────────────────────────
        header = QHBoxLayout()
        back_btn = QPushButton("\u2190  Back")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(f"""
            QPushButton {{
                font-family: {CX_FONT_SANS};
                font-size: 13px; font-weight: 500;
                color: {CX_TEXT_SECONDARY};
                background: transparent; border: none;
                padding: 4px 0;
            }}
            QPushButton:hover {{ color: {CX_TEXT}; }}
        """)
        back_btn.clicked.connect(self._on_back)
        header.addWidget(back_btn)
        header.addStretch()
        layout.addLayout(header)

        title = QLabel("Settings")
        title.setStyleSheet(PAGE_TITLE_QSS)
        layout.addWidget(title)

        # ── Sensing ──────────────────────────────────────────────────
        sensing_label = QLabel("SENSING")
        sensing_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(sensing_label)

        sensing_card = self._make_card()
        sensing_inner = QVBoxLayout(sensing_card)
        sensing_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        sensing_inner.setSpacing(SP3)

        self._webcam_enabled = QCheckBox("Enable webcam")
        self._webcam_enabled.setChecked(True)
        self._webcam_enabled.setStyleSheet(_CHECKBOX_QSS)
        sensing_inner.addWidget(self._webcam_enabled)

        self._input_telemetry_enabled = QCheckBox("Enable keyboard & mouse tracking")
        self._input_telemetry_enabled.setChecked(True)
        self._input_telemetry_enabled.setStyleSheet(_CHECKBOX_QSS)
        sensing_inner.addWidget(self._input_telemetry_enabled)

        layout.addWidget(sensing_card)

        # ── Interventions ────────────────────────────────────────────
        int_label = QLabel("INTERVENTIONS")
        int_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(int_label)

        int_card = self._make_card()
        int_inner = QVBoxLayout(int_card)
        int_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        int_inner.setSpacing(SP3)

        self._interventions_enabled = QCheckBox("Enable auto-interventions")
        self._interventions_enabled.setChecked(True)
        self._interventions_enabled.setStyleSheet(_CHECKBOX_QSS)
        int_inner.addWidget(self._interventions_enabled)

        # Sensitivity
        sens_row = QHBoxLayout()
        sens_label = QLabel("Sensitivity")
        sens_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; color: {CX_TEXT};"
        )
        sens_row.addWidget(sens_label)
        sens_row.addStretch()

        self._sensitivity_label = QLabel("3")
        self._sensitivity_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; "
            f"font-weight: 600; color: {CX_ACCENT}; "
            f"min-width: 20px;"
        )
        self._sensitivity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sens_row.addWidget(self._sensitivity_label)
        int_inner.addLayout(sens_row)

        self._sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self._sensitivity_slider.setRange(1, 5)
        self._sensitivity_slider.setValue(3)
        self._sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._sensitivity_slider.setTickInterval(1)
        self._sensitivity_slider.setStyleSheet(_SLIDER_QSS)
        self._sensitivity_slider.valueChanged.connect(
            lambda v: self._sensitivity_label.setText(str(v))
        )
        int_inner.addWidget(self._sensitivity_slider)

        sens_desc = QLabel("1 = fewer interventions  \u00b7  5 = intervene earlier")
        sens_desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; color: {CX_TEXT_TERTIARY};"
        )
        int_inner.addWidget(sens_desc)

        # Divider
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: {CX_BORDER};")
        int_inner.addWidget(div)

        # Cooldown
        cd_row = QHBoxLayout()
        cd_label = QLabel("Cooldown")
        cd_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; color: {CX_TEXT};"
        )
        cd_row.addWidget(cd_label)
        cd_row.addStretch()
        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(10, 600)
        self._cooldown_spin.setValue(60)
        self._cooldown_spin.setSuffix(" sec")
        self._cooldown_spin.setFixedWidth(100)
        self._cooldown_spin.setStyleSheet(_SPINBOX_QSS)
        cd_row.addWidget(self._cooldown_spin)
        int_inner.addLayout(cd_row)

        # Quiet mode
        self._quiet_mode = QCheckBox("Quiet mode")
        self._quiet_mode.setChecked(False)
        self._quiet_mode.setStyleSheet(_CHECKBOX_QSS)
        int_inner.addWidget(self._quiet_mode)

        qd_row = QHBoxLayout()
        qd_label = QLabel("Quiet duration")
        qd_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; color: {CX_TEXT_SECONDARY};"
        )
        qd_row.addWidget(qd_label)
        qd_row.addStretch()
        self._quiet_duration = QSpinBox()
        self._quiet_duration.setRange(5, 120)
        self._quiet_duration.setValue(30)
        self._quiet_duration.setSuffix(" min")
        self._quiet_duration.setFixedWidth(100)
        self._quiet_duration.setStyleSheet(_SPINBOX_QSS)
        qd_row.addWidget(self._quiet_duration)
        int_inner.addLayout(qd_row)

        layout.addWidget(int_card)

        # ── LLM Backend ──────────────────────────────────────────────
        llm_label = QLabel("LLM BACKEND")
        llm_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(llm_label)

        llm_card = self._make_card()
        llm_inner = QVBoxLayout(llm_card)
        llm_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        llm_inner.setSpacing(SP3)

        self._llm_backend = QComboBox()
        self._llm_backend.addItems([
            "Azure OpenAI",
            "Local (Ollama)",
            "Rule-based (no LLM)",
            "Remote (dev only)",
        ])
        self._llm_backend.setStyleSheet(_COMBO_QSS)
        llm_inner.addWidget(self._llm_backend)

        layout.addWidget(llm_card)

        # ── Debug ────────────────────────────────────────────────────
        debug_label = QLabel("DEBUG")
        debug_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(debug_label)

        debug_card = self._make_card()
        debug_inner = QVBoxLayout(debug_card)
        debug_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        debug_inner.setSpacing(SP2)

        self._debug_capture = QCheckBox("Capture debug")
        self._debug_rppg = QCheckBox("rPPG debug")
        self._debug_state = QCheckBox("State debug")
        self._debug_llm = QCheckBox("LLM debug")

        for cb in (self._debug_capture, self._debug_rppg, self._debug_state, self._debug_llm):
            cb.setStyleSheet(_CHECKBOX_QSS)
            debug_inner.addWidget(cb)

        layout.addWidget(debug_card)

        # ── Action buttons ───────────────────────────────────────────
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(SP3)
        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(BTN_GHOST_QSS)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)

        apply_btn = QPushButton("Apply")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setFixedHeight(36)
        apply_btn.setStyleSheet(BTN_PRIMARY_QSS)
        apply_btn.clicked.connect(self._apply_settings)
        btn_row.addWidget(apply_btn)

        layout.addLayout(btn_row)

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ {CARD_QSS} }}")
        return card

    def _on_back(self) -> None:
        self.hide()
        self.back_requested.emit()

    def get_settings(self) -> dict:
        """Get current settings as a dict."""
        # Map sensitivity (1-5) to threshold adjustments
        sensitivity = self._sensitivity_slider.value()
        # Sensitivity 1 = threshold 0.95, 3 = 0.85, 5 = 0.75
        threshold_offset = (3 - sensitivity) * 0.05

        # Map LLM backend combo to mode string
        llm_modes = ["azure", "local", "rule_based", "remote"]
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
