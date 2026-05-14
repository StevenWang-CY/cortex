"""Desktop Shell — Settings Dialog (macOS-native refactor).

Settings panel for Cortex configuration. Visual layer adopts:

* Sentence-case section headings (HIG) — was previously SHOUTING + letter-spaced
* SF system fonts via :func:`mac_native.system_font`
* Hairline-bordered inset cards (no Material drop shadows)
* Brand-accent (terracotta) only on the sensitivity slider handle and the
  primary "Apply" button — the rest sits in the macOS semantic palette
* Native window chrome via :func:`mac_native.apply_unified_titlebar` +
  :func:`mac_native.apply_vibrancy`

Public API preserved verbatim: ``settings_changed(dict)`` Signal,
``back_requested()`` Signal, ``get_settings``, ``_apply_settings``,
``apply_payload``, QSettings persistence under ``("Cortex", "Desktop")``.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, Signal
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

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_REGULAR,
    FW_SEMIBOLD,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
)

logger = logging.getLogger(__name__)

_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = "#5C5854"
_LABEL_TERTIARY = "#827971"
_SEPARATOR = SEMANTIC_LIGHT["separator"]


# ---------------------------------------------------------------------------
# Shared form-control QSS — minimal overrides; we let macOS render natively
# wherever possible and only style for the brand accent.
# ---------------------------------------------------------------------------

_CHECKBOX_QSS = (
    "QCheckBox {"
    f"  font-size: {FS_FOOTNOTE}px;"
    f"  color: {_LABEL};"
    "  spacing: 8px;"
    "  background: transparent;"
    "}"
)

_COMBO_QSS = (
    "QComboBox {"
    f"  font-size: {FS_FOOTNOTE}px;"
    f"  color: {_LABEL};"
    f"  background: {_CONTROL_BG};"
    f"  border: 0.5px solid {_SEPARATOR};"
    f"  border-radius: {RADIUS_BUTTON}px;"
    "  padding: 6px 12px;"
    "}"
    f"QComboBox:hover {{ border-color: rgba(0,0,0,0.20); }}"
    "QComboBox::drop-down { border: none; width: 22px; }"
)

_SPINBOX_QSS = (
    "QSpinBox {"
    f"  font-size: {FS_FOOTNOTE}px;"
    f"  color: {_LABEL};"
    f"  background: {_CONTROL_BG};"
    f"  border: 0.5px solid {_SEPARATOR};"
    f"  border-radius: {RADIUS_BUTTON}px;"
    "  padding: 4px 10px;"
    "}"
)

_SLIDER_QSS = (
    f"QSlider::groove:horizontal {{ background: {_GROUPED_BG};"
    " height: 4px; border-radius: 2px; }}"
    f"QSlider::handle:horizontal {{ background: {BRAND_ACCENT};"
    " width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }}"
    f"QSlider::handle:horizontal:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
    f"QSlider::sub-page:horizontal {{ background: {BRAND_ACCENT};"
    " border-radius: 2px; }}"
)

_PAGE_TITLE_QSS = (
    f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
    f"font-size: {FS_TITLE}px;"
    "font-style: italic;"
    f"font-weight: {FW_REGULAR};"
    f"color: {_LABEL};"
    "background: transparent;"
)

_SECTION_HEADING_QSS = (
    f"font-size: {FS_FOOTNOTE}px;"
    f"font-weight: {FW_SEMIBOLD};"
    f"color: {_LABEL_SECONDARY};"
    "background: transparent;"
)


class SettingsDialog(QWidget):
    """Settings dialog. Emits ``settings_changed(dict)`` on Apply."""

    settings_changed = Signal(dict)
    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Settings")
        self.setMinimumSize(440, 580)
        self.setStyleSheet(f"background: {_WINDOW_BG}; color: {_LABEL};")
        # E.2: QSettings persistence — Cortex/Desktop. Default values come
        # from the widget initializers in _build_ui; we restore over the
        # top once the UI is built.
        self._qs = QSettings("Cortex", "Desktop")
        self._build_ui()
        self._load_persisted_settings()

    # -- Native chrome ---------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="window_background")
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP5)

        # ── Header (back link + page title) ──────────────────────────
        header = QHBoxLayout()
        back_btn = QPushButton("←  Back")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        back_btn.setStyleSheet(
            "QPushButton {"
            f"  color: {_LABEL_SECONDARY};"
            "  background: transparent; border: none; padding: 4px 0;"
            "}"
            f"QPushButton:hover {{ color: {_LABEL}; }}"
        )
        back_btn.clicked.connect(self._on_back)
        header.addWidget(back_btn)
        header.addStretch()
        layout.addLayout(header)

        title = QLabel("Settings")
        title.setStyleSheet(_PAGE_TITLE_QSS)
        layout.addWidget(title)

        # ── Sensing ──────────────────────────────────────────────────
        sensing_label = QLabel("Sensing")
        sensing_label.setStyleSheet(_SECTION_HEADING_QSS)
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
        int_label = QLabel("Interventions")
        int_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(int_label)

        int_card = self._make_card()
        int_inner = QVBoxLayout(int_card)
        int_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        int_inner.setSpacing(SP3)

        self._interventions_enabled = QCheckBox("Enable auto-interventions")
        self._interventions_enabled.setChecked(True)
        self._interventions_enabled.setStyleSheet(_CHECKBOX_QSS)
        int_inner.addWidget(self._interventions_enabled)

        # Sensitivity row — label left, current value right (HIG pattern).
        sens_row = QHBoxLayout()
        sens_label = QLabel("Sensitivity")
        sens_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        sens_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        sens_row.addWidget(sens_label)
        sens_row.addStretch()

        self._sensitivity_label = QLabel("3")
        self._sensitivity_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._sensitivity_label.setStyleSheet(
            f"color: {BRAND_ACCENT}; min-width: 20px; background: transparent;"
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

        sens_desc = QLabel("1 = fewer interventions  ·  5 = intervene earlier")
        sens_desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        sens_desc.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        int_inner.addWidget(sens_desc)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: {_SEPARATOR};")
        int_inner.addWidget(div)

        cd_row = QHBoxLayout()
        cd_label = QLabel("Cooldown")
        cd_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        cd_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        cd_row.addWidget(cd_label)
        cd_row.addStretch()
        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(10, 600)
        self._cooldown_spin.setValue(60)
        self._cooldown_spin.setSuffix(" sec")
        self._cooldown_spin.setMinimumWidth(96)
        self._cooldown_spin.setStyleSheet(_SPINBOX_QSS)
        cd_row.addWidget(self._cooldown_spin)
        int_inner.addLayout(cd_row)

        self._quiet_mode = QCheckBox("Quiet mode")
        self._quiet_mode.setChecked(False)
        self._quiet_mode.setStyleSheet(_CHECKBOX_QSS)
        int_inner.addWidget(self._quiet_mode)

        qd_row = QHBoxLayout()
        qd_label = QLabel("Quiet duration")
        qd_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        qd_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        qd_row.addWidget(qd_label)
        qd_row.addStretch()
        self._quiet_duration = QSpinBox()
        self._quiet_duration.setRange(5, 120)
        self._quiet_duration.setValue(30)
        self._quiet_duration.setSuffix(" min")
        self._quiet_duration.setMinimumWidth(96)
        self._quiet_duration.setStyleSheet(_SPINBOX_QSS)
        qd_row.addWidget(self._quiet_duration)
        int_inner.addLayout(qd_row)

        layout.addWidget(int_card)

        # ── LLM Backend ──────────────────────────────────────────────
        llm_label = QLabel("LLM backend")
        llm_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(llm_label)

        llm_card = self._make_card()
        llm_inner = QVBoxLayout(llm_card)
        llm_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        llm_inner.setSpacing(SP3)

        self._llm_backend = QComboBox()
        self._llm_backend.addItems([
            "AWS Bedrock (Anthropic)",
            "Google Vertex (Anthropic)",
            "Anthropic API (direct)",
            "Rule-based (offline)",
        ])
        self._llm_backend.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._llm_backend.setStyleSheet(_COMBO_QSS)
        llm_inner.addWidget(self._llm_backend)

        layout.addWidget(llm_card)

        # ── Debug ────────────────────────────────────────────────────
        debug_label = QLabel("Debug")
        debug_label.setStyleSheet(_SECTION_HEADING_QSS)
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

        # ── Actions (HIG: bottom-right, Cancel/Close left of Apply) ──
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(SP3)
        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setMinimumHeight(32)
        close_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        close_btn.setStyleSheet(
            "QPushButton {"
            f"  padding: 6px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL_SECONDARY};"
            f"  border: 0.5px solid {_SEPARATOR};"
            "}"
            "QPushButton:hover { background: rgba(0,0,0,0.03); color: " + _LABEL + "; }"
        )
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)

        apply_btn = QPushButton("Apply")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setMinimumHeight(32)
        apply_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        # HIG default action: filled with the brand accent. macOS' own default
        # action would use NSColor.controlAccentColor; we override with our
        # terracotta to keep the brand mark.
        apply_btn.setStyleSheet(
            "QPushButton {"
            f"  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF;"
            "  border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
            "QPushButton:pressed { background: #B05439; }"
        )
        apply_btn.clicked.connect(self._apply_settings)
        btn_row.addWidget(apply_btn)

        layout.addLayout(btn_row)

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            "QFrame {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        return card

    def _on_back(self) -> None:
        self.hide()
        self.back_requested.emit()

    # ------------------------------------------------------------------
    # API surface (preserved byte-identical from pre-refactor)
    # ------------------------------------------------------------------

    def get_settings(self) -> dict:
        """Return current settings as a dict."""
        sensitivity = self._sensitivity_slider.value()
        threshold_offset = (3 - sensitivity) * 0.05

        # Map LLM backend combo to mode string. Order must match the
        # addItems(...) call in _build_ui and the LLMConfig.provider
        # Literal in cortex.libs.config.settings.
        llm_modes = ["bedrock", "vertex", "direct", "rule_based"]
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
        settings = self.get_settings()
        # Persist before emitting so a crash mid-roundtrip doesn't lose
        # the user's choices.
        self._persist_settings(settings)
        self.settings_changed.emit(settings)
        logger.info(
            "Settings applied: sensitivity=%s llm=%s",
            settings["sensitivity"], settings["llm_mode"],
        )

    def _persist_settings(self, settings: dict) -> None:
        for key, value in settings.items():
            try:
                self._qs.setValue(key, value)
            except Exception:
                pass
        try:
            self._qs.sync()
        except Exception:
            pass

    def _load_persisted_settings(self) -> None:
        def _get_bool(key: str, default: bool) -> bool:
            v = self._qs.value(key, default)
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in {"true", "1", "yes"}
            return bool(v)

        def _get_int(key: str, default: int) -> int:
            v = self._qs.value(key, default)
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        try:
            self._webcam_enabled.setChecked(_get_bool("webcam_enabled", True))
            self._input_telemetry_enabled.setChecked(
                _get_bool("input_telemetry_enabled", True)
            )
            self._interventions_enabled.setChecked(
                _get_bool("interventions_enabled", True)
            )
            self._sensitivity_slider.setValue(_get_int("sensitivity", 3))
            self._cooldown_spin.setValue(_get_int("cooldown_seconds", 60))
            self._quiet_mode.setChecked(_get_bool("quiet_mode", False))
            self._quiet_duration.setValue(_get_int("quiet_duration_minutes", 30))
            llm_mode = str(self._qs.value("llm_mode", "bedrock"))
            llm_modes = ["bedrock", "vertex", "direct", "rule_based"]
            if llm_mode in llm_modes:
                self._llm_backend.setCurrentIndex(llm_modes.index(llm_mode))
            self._debug_capture.setChecked(_get_bool("debug_capture", False))
            self._debug_rppg.setChecked(_get_bool("debug_rppg", False))
            self._debug_state.setChecked(_get_bool("debug_state", False))
            self._debug_llm.setChecked(_get_bool("debug_llm", False))
        except Exception:
            logger.debug("Failed to restore persisted settings", exc_info=True)

    def apply_payload(self, payload: dict) -> None:
        """Apply a ``SETTINGS_SYNC`` payload arriving from the daemon."""
        if not isinstance(payload, dict):
            return
        try:
            if "quiet_mode" in payload:
                self._quiet_mode.setChecked(bool(payload["quiet_mode"]))
            if "quiet_duration_minutes" in payload:
                try:
                    self._quiet_duration.setValue(int(payload["quiet_duration_minutes"]))
                except (TypeError, ValueError):
                    pass
            if "sensitivity" in payload:
                try:
                    self._sensitivity_slider.setValue(int(payload["sensitivity"]))
                except (TypeError, ValueError):
                    pass
            if "interventions_enabled" in payload:
                self._interventions_enabled.setChecked(
                    bool(payload["interventions_enabled"])
                )
            if "webcam_enabled" in payload:
                self._webcam_enabled.setChecked(bool(payload["webcam_enabled"]))
        except Exception:
            logger.debug("Failed to apply SETTINGS_SYNC payload", exc_info=True)
        self._persist_settings(self.get_settings())
