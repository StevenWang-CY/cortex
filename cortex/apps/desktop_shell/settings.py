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
import time
from pathlib import Path

from PySide6.QtCore import QMutex, QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.a11y import (
    chain_tab_order,
    set_accessible_description,
    set_accessible_name,
)
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
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
# WCAG-AA-passing label tints from the token registry — was carrying a
# private sub-AA copy that drifted from the dashboard's audit-F55 fix.
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY
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


def _baseline_default_path() -> Path:
    """Return the path to `storage/baselines/default.json`.

    Lazy-imported config so the settings dialog stays importable even
    when the config layer is mocked out by the test harness.
    """
    try:
        from cortex.libs.config.settings import get_config

        return Path(get_config().storage.path) / "baselines" / "default.json"
    except Exception:
        return Path("storage") / "baselines" / "default.json"


def _format_relative_time(mtime: float, now: float | None = None) -> str:
    """Human-readable "N days ago" / "2 hours ago" formatter — matches
    the dashboard's freshness pill so the two surfaces stay consistent."""
    if mtime <= 0:
        return "never"
    current = now if now is not None else time.time()
    delta = max(0.0, current - mtime)
    minute = 60.0
    hour = 60.0 * minute
    day = 24.0 * hour
    if delta < minute:
        return "just now"
    if delta < hour:
        n = int(delta // minute)
        return f"{n} minute ago" if n == 1 else f"{n} minutes ago"
    if delta < day:
        n = int(delta // hour)
        return f"{n} hour ago" if n == 1 else f"{n} hours ago"
    n = int(delta // day)
    return f"{n} day ago" if n == 1 else f"{n} days ago"


def _format_baseline_freshness_label(path: Path | None = None) -> str:
    """Label text used by the Sensing section's recalibrate row.

    Returns "Last calibrated: <relative time>" when a baseline file is
    present, or a softer prompt to calibrate when it isn't.
    """
    target = path or _baseline_default_path()
    try:
        if not target.exists():
            return "Last calibrated: never · calibrate for personal baselines"
        mtime = target.stat().st_mtime
        return f"Last calibrated: {_format_relative_time(mtime)}"
    except OSError:
        return "Last calibrated: unknown"


class SettingsDialog(QWidget):
    """Settings dialog. Emits ``settings_changed(dict)`` on Apply."""

    settings_changed = Signal(dict)
    back_requested = Signal()
    # F53: surfaced when QSettings.sync() fails (read-only filesystem,
    # disk full, sandbox container ACL). Controller subscribes and shows
    # the failure to the user via a toast / dialog rather than letting it
    # disappear into the prior bare ``except: pass``.
    settings_save_failed = Signal(str)
    # Audit Debt-2 Commit 5: emitted after the user rotates the
    # capability token via the Security section. Controller listens
    # to refresh ``WebSocketBridge._auth_token`` and to surface a
    # confirmation toast.
    auth_token_rotated = Signal(str)
    # P0 §3.4: emitted when the user clicks "Recalibrate baselines" in
    # the Sensing section. Controller routes to the same in-process
    # CalibrationRunner code path as the onboarding wizard.
    recalibrate_requested = Signal()
    # P0 §3.19: emitted when the user clicks "Test connection" in the
    # LLM backend section. Phase 4b's daemon handles TEST_PROVIDER → the
    # desktop renders the result via :meth:`apply_provider_test_result`.
    # Payload is the current provider key (e.g. "bedrock", "vertex",
    # "anthropic", "rule_based").
    test_provider_requested = Signal(str)
    # P0 §3.15: emitted when the user changes the daily LLM cap.
    # Controller (or Phase 4b daemon's settings sync) consumes the
    # value via the SETTINGS_SYNC channel. Payload is USD.
    daily_budget_changed = Signal(float)
    # P0 §3.25: emitted when the user picks a new accessibility
    # palette in the dropdown. Payload is the palette name
    # ("default" / "deuteranopia" / "protanopia" / "tritanopia").
    palette_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Settings")
        self.setMinimumSize(440, 580)
        self.setStyleSheet(f"background: {_WINDOW_BG}; color: {_LABEL};")
        # E.2: QSettings persistence — Cortex/Desktop. Default values come
        # from the widget initializers in _build_ui; we restore over the
        # top once the UI is built.
        self._qs = QSettings("Cortex", "Desktop")
        # F04: mutex guards _apply_settings against double-click reentrancy.
        # The Apply button is also disabled for the duration of the apply so
        # the user sees the UI as busy, not silently failing.
        self._apply_mutex: QMutex = QMutex()
        # Monotonic counter — every Apply increments. The daemon's
        # _handle_settings_sync rejects any payload whose version is older
        # than the last one it accepted, so a coalesced double-click cannot
        # land out-of-order behind an in-flight earlier apply.
        self._settings_version: int = 0
        self._apply_btn: QPushButton | None = None
        self._build_ui()
        self._load_persisted_settings()

        # macOS Privacy & Security grants are made out-of-process — there is
        # no callback into the app when the user flips the toggle. Poll
        # every 1.5s while the dialog is visible so the camera /
        # accessibility status pills reflect reality without a relaunch
        # or "Check again" click. Paused on hide via ``hideEvent``.
        self._permission_timer: QTimer = QTimer(self)
        self._permission_timer.setInterval(1500)
        self._permission_timer.timeout.connect(self._refresh_permission_states)
        self._permission_timer.start()
        self._refresh_permission_states()

    # -- Native chrome ---------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="window_background")
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)
        # Resume + force-refresh the permission poll whenever the dialog
        # becomes visible. ``_permission_timer`` may have been stopped by
        # a prior hide; restart it so users opening Settings always see
        # the current grant state on the first frame, not 1.5s later.
        try:
            if not self._permission_timer.isActive():
                self._permission_timer.start()
            self._refresh_permission_states()
        except Exception:
            logger.debug("permission poll resume failed", exc_info=True)
        # P0 §3.4: also refresh the baseline freshness label so a
        # recalibration completed elsewhere reflects on re-open.
        try:
            self.refresh_baseline_freshness()
        except Exception:
            logger.debug("freshness refresh on show failed", exc_info=True)

    def hideEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        # Stop polling when the dialog is not visible — saves ~40 syscalls/
        # minute on TCC + AVCaptureDevice queries and prevents background
        # work from spinning when Cortex is minimised.
        try:
            self._permission_timer.stop()
        except Exception:
            logger.debug("permission poll stop failed", exc_info=True)
        super().hideEvent(event)

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

        # macOS Privacy & Security status pill, polled every 1.5s while
        # the dialog is visible. Clicking opens the relevant pane in
        # System Settings. Pre-fix this status only existed in the
        # onboarding wizard; users who flipped the toggle from System
        # Settings mid-session had no in-app feedback until relaunch.
        self._camera_perm_row = self._make_permission_row(
            label_not_granted="Camera access not granted — open System Settings",
            label_granted="Camera access granted",
            target="camera",
        )
        sensing_inner.addLayout(self._camera_perm_row["layout"])

        self._input_telemetry_enabled = QCheckBox("Enable keyboard & mouse tracking")
        self._input_telemetry_enabled.setChecked(True)
        self._input_telemetry_enabled.setStyleSheet(_CHECKBOX_QSS)
        sensing_inner.addWidget(self._input_telemetry_enabled)

        self._accessibility_perm_row = self._make_permission_row(
            label_not_granted="Accessibility access not granted — open System Settings",
            label_granted="Accessibility access granted",
            target="accessibility",
        )
        sensing_inner.addLayout(self._accessibility_perm_row["layout"])

        # P0 §3.4: Recalibrate baselines row. Shows "Last calibrated:
        # <relative time>" next to a brand-accent button. Clicking
        # emits ``recalibrate_requested`` which the controller routes
        # to the same in-process CalibrationRunner the onboarding
        # wizard drives.
        recal_row = QHBoxLayout()
        recal_row.setSpacing(SP3)
        self._baseline_freshness_label = QLabel(
            _format_baseline_freshness_label()
        )
        self._baseline_freshness_label.setFont(
            mac_native.system_font(FS_CAPTION, "regular")
        )
        self._baseline_freshness_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent; border: none;"
        )
        recal_row.addWidget(self._baseline_freshness_label)
        recal_row.addStretch()

        self._recalibrate_btn = QPushButton("Recalibrate baselines")
        self._recalibrate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recalibrate_btn.setMinimumHeight(28)
        self._recalibrate_btn.setFont(
            mac_native.system_font(FS_CAPTION, "semibold")
        )
        self._recalibrate_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 4px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        self._recalibrate_btn.clicked.connect(self.recalibrate_requested.emit)
        set_accessible_name(self._recalibrate_btn, "Recalibrate baselines")
        set_accessible_description(
            self._recalibrate_btn,
            "Re-run the 2-minute calibration capture to refresh your personal baselines.",
        )
        recal_row.addWidget(self._recalibrate_btn)
        sensing_inner.addLayout(recal_row)

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

        # ── Focus Protection (P0 §3.10) ──────────────────────────────
        fp_label = QLabel("Focus protection")
        fp_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(fp_label)

        fp_card = self._make_card()
        fp_inner = QVBoxLayout(fp_card)
        fp_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        fp_inner.setSpacing(SP3)

        self._auto_distraction_block = QCheckBox(
            "Auto-arm focus session when overwhelmed",
        )
        self._auto_distraction_block.setChecked(False)
        self._auto_distraction_block.setStyleSheet(_CHECKBOX_QSS)
        fp_inner.addWidget(self._auto_distraction_block)

        fp_help = QLabel(
            "When Cortex detects sustained overwhelm (HYPER state), "
            "block known-distracting sites for a focus session. You "
            "can disarm at any time."
        )
        fp_help.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        fp_help.setWordWrap(True)
        fp_help.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        fp_inner.addWidget(fp_help)

        preset_row = QHBoxLayout()
        preset_label = QLabel("Blocklist preset")
        preset_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        preset_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        preset_row.addWidget(preset_label)
        preset_row.addStretch()
        self._distraction_preset = QComboBox()
        self._distraction_preset.addItems([
            "Developer",
            "Student",
            "Writer",
            "Custom",
        ])
        self._distraction_preset.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._distraction_preset.setStyleSheet(_COMBO_QSS)
        preset_row.addWidget(self._distraction_preset)
        fp_inner.addLayout(preset_row)

        custom_label = QLabel("Custom domains (one per line)")
        custom_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        custom_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        fp_inner.addWidget(custom_label)

        self._distraction_custom_domains = QPlainTextEdit()
        self._distraction_custom_domains.setPlaceholderText(
            "example.com\nanother-distraction.com"
        )
        self._distraction_custom_domains.setFont(
            mac_native.system_font(FS_CAPTION, "regular"),
        )
        self._distraction_custom_domains.setMaximumHeight(90)
        self._distraction_custom_domains.setStyleSheet(
            "QPlainTextEdit {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  color: {_LABEL};"
            "  padding: 6px;"
            "}"
        )
        fp_inner.addWidget(self._distraction_custom_domains)

        layout.addWidget(fp_card)

        # ── Notifications (P0 §3.12) ────────────────────────────────
        notif_label = QLabel("Notifications")
        notif_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(notif_label)

        notif_card = self._make_card()
        notif_inner = QVBoxLayout(notif_card)
        notif_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        notif_inner.setSpacing(SP2)

        self._os_notifications = QCheckBox(
            "Send macOS notifications when Cortex is in the background",
        )
        self._os_notifications.setChecked(True)
        self._os_notifications.setStyleSheet(_CHECKBOX_QSS)
        notif_inner.addWidget(self._os_notifications)

        notif_help = QLabel(
            "When the dashboard isn't your active window, route "
            "interventions through macOS Notification Center so you "
            "don't miss them from another Space or fullscreen app."
        )
        notif_help.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        notif_help.setWordWrap(True)
        notif_help.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        notif_inner.addWidget(notif_help)

        layout.addWidget(notif_card)

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

        # P0 §3.19: provider status pill + "Test connection" button.
        # The pill shows the last known result of a test, e.g.
        # "Bedrock ✓ 142 ms" or "Vertex ✗ token expired". Phase 4b
        # owns the WS messages TEST_PROVIDER / TEST_PROVIDER_RESULT; we
        # stub the desktop UI and document the contract.
        provider_row = QHBoxLayout()
        provider_row.setContentsMargins(0, 0, 0, 0)
        provider_row.setSpacing(SP2)
        self._provider_status_pill = QLabel("Status unknown")
        self._provider_status_pill.setFont(
            mac_native.system_font(FS_CAPTION, "regular"),
        )
        self._provider_status_pill.setStyleSheet(
            "QLabel {"
            "  padding: 2px 10px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  color: {_LABEL_SECONDARY};"
            f"  background: {_GROUPED_BG};"
            "}"
        )
        provider_row.addWidget(self._provider_status_pill, stretch=1)
        self._test_provider_btn = QPushButton("Test connection")
        self._test_provider_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test_provider_btn.setFont(
            mac_native.system_font(FS_CAPTION, "medium"),
        )
        self._test_provider_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 4px 10px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL};"
            f"  border: 0.5px solid {_SEPARATOR};"
            "}"
            "QPushButton:hover { background: rgba(0,0,0,0.03); }"
        )
        self._test_provider_btn.clicked.connect(self._on_test_provider_clicked)
        provider_row.addWidget(self._test_provider_btn)
        llm_inner.addLayout(provider_row)

        layout.addWidget(llm_card)

        # ── Budget (P0 §3.15) ────────────────────────────────────────
        budget_label = QLabel("Budget")
        budget_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(budget_label)

        budget_card = self._make_card()
        budget_inner = QVBoxLayout(budget_card)
        budget_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        budget_inner.setSpacing(SP3)

        # Daily cap row. ``$ X.XX / day`` — anything > 0 enables the
        # daemon's kill-switch.
        cap_row = QHBoxLayout()
        cap_label = QLabel("Daily LLM cap")
        cap_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        cap_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        cap_row.addWidget(cap_label)
        cap_row.addStretch()
        self._budget_daily_spin = QDoubleSpinBox()
        self._budget_daily_spin.setDecimals(2)
        self._budget_daily_spin.setRange(0.0, 50.0)
        self._budget_daily_spin.setSingleStep(0.25)
        self._budget_daily_spin.setValue(1.50)
        self._budget_daily_spin.setSuffix(" USD")
        self._budget_daily_spin.setMinimumWidth(96)
        cap_row.addWidget(self._budget_daily_spin)
        budget_inner.addLayout(cap_row)

        self._budget_today_label = QLabel("Cost so far today: pending…")
        self._budget_today_label.setFont(
            mac_native.system_font(FS_CAPTION, "regular"),
        )
        self._budget_today_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        budget_inner.addWidget(self._budget_today_label)

        budget_help = QLabel(
            "Cortex pauses Claude calls and falls back to the rule-based "
            "planner once the daily cap is reached. 0 disables the cap."
        )
        budget_help.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        budget_help.setWordWrap(True)
        budget_help.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        budget_inner.addWidget(budget_help)

        layout.addWidget(budget_card)

        # ── Accessibility (P0 §3.25) ─────────────────────────────────
        a11y_label = QLabel("Accessibility")
        a11y_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(a11y_label)
        a11y_card = self._make_card()
        a11y_inner = QVBoxLayout(a11y_card)
        a11y_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        a11y_inner.setSpacing(SP3)

        palette_row = QHBoxLayout()
        palette_label = QLabel("Color palette")
        palette_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        palette_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        palette_row.addWidget(palette_label)
        palette_row.addStretch()
        self._palette_combo = QComboBox()
        self._palette_combo.addItem("Default", "default")
        self._palette_combo.addItem("Deuteranopia (red/green)", "deuteranopia")
        self._palette_combo.addItem("Protanopia", "protanopia")
        self._palette_combo.addItem("Tritanopia", "tritanopia")
        self._palette_combo.setFont(
            mac_native.system_font(FS_FOOTNOTE, "regular"),
        )
        self._palette_combo.setStyleSheet(_COMBO_QSS)
        try:
            self._palette_combo.activated.connect(self._on_palette_changed)
        except Exception:
            logger.debug("palette combo connect failed", exc_info=True)
        palette_row.addWidget(self._palette_combo)
        a11y_inner.addLayout(palette_row)

        a11y_help = QLabel(
            "Swap the state palette to a color-blind-safe variant. "
            "Restart Cortex to apply the new colors everywhere."
        )
        a11y_help.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        a11y_help.setWordWrap(True)
        a11y_help.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        a11y_inner.addWidget(a11y_help)
        layout.addWidget(a11y_card)

        # ── Weekly schedule (P0 §3.20) ───────────────────────────────
        sched_label = QLabel("Weekly schedule")
        sched_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(sched_label)
        sched_card = self._make_card()
        sched_inner = QVBoxLayout(sched_card)
        sched_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        sched_inner.setSpacing(SP2)

        # 7 rows (Mon-Sun), 4 time slots each (morning / midday /
        # afternoon / evening). Each slot is a small QComboBox with
        # ``on`` / ``quiet`` / ``off`` so the daemon can consult the
        # schedule before surfacing interventions outside the working
        # window. The schedule is persisted into QSettings under
        # ``weekly_schedule`` (a JSON-encoded dict).
        self._schedule_combos: dict[str, list[QComboBox]] = {}
        self._SCHEDULE_DAYS = (
            "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday", "sunday",
        )
        self._SCHEDULE_SLOTS = ("morning", "midday", "afternoon", "evening")
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel(""), stretch=1)
        for slot in self._SCHEDULE_SLOTS:
            lab = QLabel(slot.title())
            lab.setFont(mac_native.system_font(FS_CAPTION, "medium"))
            lab.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            header_row.addWidget(lab, stretch=1)
        sched_inner.addLayout(header_row)
        for day in self._SCHEDULE_DAYS:
            row = QHBoxLayout()
            day_label = QLabel(day[:3].title())
            day_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            day_label.setStyleSheet(
                f"color: {_LABEL}; background: transparent;"
            )
            row.addWidget(day_label, stretch=1)
            combos: list[QComboBox] = []
            for _ in self._SCHEDULE_SLOTS:
                cb = QComboBox()
                cb.addItems(["on", "quiet", "off"])
                cb.setStyleSheet(_COMBO_QSS)
                combos.append(cb)
                row.addWidget(cb, stretch=1)
            self._schedule_combos[day] = combos
            sched_inner.addLayout(row)
        layout.addWidget(sched_card)

        # ── Security ────────────────────────────────────────────────
        # Audit Debt-2 Commit 5: the capability token gates every
        # HTTP/WS request the daemon accepts. Rotation invalidates the
        # current token, forces every connected client to re-AUTH with
        # the new value, and is the user-visible escape hatch for
        # "someone might know my token" (shared-machine flag).
        sec_label = QLabel("Security")
        sec_label.setStyleSheet(_SECTION_HEADING_QSS)
        layout.addWidget(sec_label)

        sec_card = self._make_card()
        sec_inner = QVBoxLayout(sec_card)
        sec_inner.setContentsMargins(SP4, SP4, SP4, SP4)
        sec_inner.setSpacing(SP2)

        rotate_btn = QPushButton("Rotate authentication token")
        rotate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rotate_btn.setMinimumHeight(32)
        rotate_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        rotate_btn.setStyleSheet(
            "QPushButton {"
            f"  padding: 6px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL};"
            f"  border: 0.5px solid {_SEPARATOR};"
            "}"
            "QPushButton:hover { background: rgba(0,0,0,0.03); }"
        )
        set_accessible_name(rotate_btn, "Rotate authentication token")
        set_accessible_description(
            rotate_btn,
            "Replaces the capability token used by the dashboard and "
            "browser extension. All currently-connected clients will "
            "reconnect with the new token automatically.",
        )
        rotate_btn.clicked.connect(self._on_rotate_token)
        sec_inner.addWidget(rotate_btn)

        sec_hint = QLabel(
            "Use if you suspect another user on this Mac may have "
            "read your auth token file."
        )
        sec_hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        sec_hint.setWordWrap(True)
        sec_hint.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        sec_inner.addWidget(sec_hint)

        self._rotate_token_btn = rotate_btn
        self._rotate_token_status = QLabel("")
        self._rotate_token_status.setFont(
            mac_native.system_font(FS_CAPTION, "regular"),
        )
        self._rotate_token_status.setWordWrap(True)
        self._rotate_token_status.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        sec_inner.addWidget(self._rotate_token_status)

        layout.addWidget(sec_card)

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
        # F04: keep a handle on the Apply button so _apply_settings can
        # disable it while an apply is in-flight (visual coalescing of
        # double-clicks; the mutex below is the correctness guarantee).
        self._apply_btn = apply_btn
        btn_row.addWidget(apply_btn)

        layout.addLayout(btn_row)

        # audit-w2 (F55 carry-over): accessible names + tab order. Without
        # these, VoiceOver announces every control as "checkbox" / "button"
        # and the focus ring skips around the panel unpredictably.
        set_accessible_name(back_btn, "Back to dashboard")
        set_accessible_name(self._webcam_enabled, "Enable webcam")
        set_accessible_description(
            self._webcam_enabled,
            "Allow Cortex to use the webcam for biometric sensing.",
        )
        set_accessible_name(
            self._input_telemetry_enabled,
            "Enable keyboard and mouse tracking",
        )
        set_accessible_name(self._interventions_enabled, "Enable interventions")
        set_accessible_name(self._sensitivity_slider, "Intervention sensitivity")
        set_accessible_description(
            self._sensitivity_slider,
            "Lower values intervene less often; higher values intervene earlier.",
        )
        set_accessible_name(self._cooldown_spin, "Intervention cooldown (seconds)")
        set_accessible_name(self._quiet_mode, "Quiet mode")
        set_accessible_name(self._quiet_duration, "Quiet duration (minutes)")
        set_accessible_name(self._llm_backend, "LLM backend provider")
        set_accessible_name(self._debug_capture, "Capture debug logging")
        set_accessible_name(self._debug_rppg, "rPPG debug logging")
        set_accessible_name(self._debug_state, "State engine debug logging")
        set_accessible_name(self._debug_llm, "LLM engine debug logging")
        set_accessible_name(close_btn, "Close settings")
        set_accessible_name(apply_btn, "Apply settings")

        # Phase-3 / Audit-1.2 F5: include the new P0 §3.10 / §3.12
        # controls in the tab order so keyboard users can reach them.
        chain_tab_order(
            back_btn,
            self._webcam_enabled,
            self._input_telemetry_enabled,
            self._interventions_enabled,
            self._sensitivity_slider,
            self._cooldown_spin,
            self._quiet_mode,
            self._quiet_duration,
            self._auto_distraction_block,
            self._distraction_preset,
            self._distraction_custom_domains,
            self._os_notifications,
            self._llm_backend,
            self._debug_capture,
            self._debug_rppg,
            self._debug_state,
            self._debug_llm,
            close_btn,
            apply_btn,
        )
        # Accessible names for the new widgets so VoiceOver announces
        # them by purpose rather than "checkbox" / "combo box".
        set_accessible_name(
            self._auto_distraction_block,
            "Auto-arm focus session when overwhelmed",
        )
        set_accessible_description(
            self._auto_distraction_block,
            "When Cortex detects sustained overwhelm (HYPER state), "
            "block known-distracting sites for a focus session.",
        )
        set_accessible_name(self._distraction_preset, "Blocklist preset")
        set_accessible_name(
            self._distraction_custom_domains,
            "Custom distraction domains, one per line",
        )
        set_accessible_name(
            self._os_notifications,
            "Send macOS notifications when Cortex is in the background",
        )

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("CortexSettingsCard")
        card.setStyleSheet(
            "QFrame#CortexSettingsCard {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        return card

    def _make_permission_row(
        self,
        *,
        label_not_granted: str,
        label_granted: str,
        target: str,
    ) -> dict:
        """Build a status row that reflects a live macOS permission grant.

        Returns a dict with:
          * ``layout`` — the QHBoxLayout to add into the parent
          * ``label`` — the QLabel updated on every poll
          * ``label_granted`` / ``label_not_granted`` — text variants
          * ``target`` — ``"camera"`` | ``"accessibility"``
          * ``granted`` — last polled state (bool)

        The row is non-interactive in the not-yet-granted state besides
        a clickable text link to ``x-apple.systempreferences:`` deep-links
        for the relevant pane. Once granted, the row collapses to a
        single-line confirmation in the secondary text colour with a
        check glyph.
        """
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(SP4, 0, 0, 0)
        row_layout.setSpacing(SP2)

        label = QLabel(label_not_granted)
        label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        label.setWordWrap(True)
        label.setOpenExternalLinks(False)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        label.linkActivated.connect(  # type: ignore[attr-defined]
            lambda _href, t=target: self._open_system_settings(t)
        )
        row_layout.addWidget(label, stretch=1)

        set_accessible_name(label, f"{target} permission status")
        set_accessible_description(
            label,
            "Reflects the current macOS Privacy & Security grant for this "
            "permission. Polled every 1.5 seconds while Settings is open.",
        )

        return {
            "layout": row_layout,
            "label": label,
            "label_granted": label_granted,
            "label_not_granted": label_not_granted,
            "target": target,
            "granted": False,
        }

    def refresh_baseline_freshness(self) -> None:
        """Re-read the baseline file's mtime and update the Sensing row
        label. P0 §3.4 — called by the controller after a successful
        calibration run completes so the user sees the new timestamp
        without re-opening the dialog."""
        label = getattr(self, "_baseline_freshness_label", None)
        if label is None:
            return
        try:
            label.setText(_format_baseline_freshness_label())
        except Exception:
            logger.debug("freshness label update failed", exc_info=True)

    def _refresh_permission_states(self) -> None:
        """Poll macOS TCC + AX for the current grant state and update
        the camera / accessibility status rows accordingly.

        Called every 1.5s by ``_permission_timer`` while the dialog is
        visible (see ``showEvent`` / ``hideEvent``). The helpers live
        in ``cortex.libs.utils.platform`` and return ``False`` on any
        Cocoa-bridge surprise, so the UI never crashes here even on
        non-macOS dev runs.
        """
        try:
            from cortex.libs.utils import (
                check_accessibility_permission,
                check_camera_permission,
            )
        except Exception:
            logger.debug("permission helpers unavailable", exc_info=True)
            return

        try:
            cam = bool(check_camera_permission())
        except Exception:
            cam = False
        try:
            acc = bool(check_accessibility_permission())
        except Exception:
            acc = False

        self._render_permission_row(self._camera_perm_row, cam)
        self._render_permission_row(self._accessibility_perm_row, acc)

    def _render_permission_row(self, row: dict, granted: bool) -> None:
        """Mutate a row to reflect a fresh poll. Guarded against repeat
        writes — when ``granted`` matches the cached state, the label /
        stylesheet are left alone so Qt's paint loop doesn't churn at
        1.5 Hz across two rows.
        """
        if row.get("granted") == granted:
            return
        row["granted"] = granted
        label: QLabel = row["label"]
        if granted:
            # Sentence-case "Granted" pill, secondary text colour, leading
            # check glyph. Not a hyperlink — there is nothing for the
            # user to do here, and an underline would invite mis-clicks.
            label.setText(f"✓ {row['label_granted']}")
            label.setStyleSheet(
                f"color: {CX_TEXT_SECONDARY}; background: transparent;"
            )
        else:
            # Embed the call-to-action as an inline link so VoiceOver
            # announces "link" and clicks deep-link to System Settings.
            # Underline + trailing chevron because WCAG 1.4.1 forbids
            # signalling interactivity by colour alone — without the
            # underline the brand-accent text reads as static caption
            # to sighted users who can't distinguish accent from body.
            target = row["target"]
            base = row["label_not_granted"]
            label.setText(
                f'<a href="cortex://open-system-settings/{target}" '
                f'style="color: {BRAND_ACCENT}; text-decoration: underline;">'
                f"{base} ›</a>"
            )
            label.setStyleSheet(
                f"color: {CX_TEXT_TERTIARY}; background: transparent;"
            )

    def _open_system_settings(self, target: str) -> None:
        """Deep-link System Settings to the correct Privacy & Security
        pane. ``target`` is one of ``"camera"`` / ``"accessibility"``.

        Uses the documented ``x-apple.systempreferences:`` URL scheme via
        ``open(1)``. Safe to call from the GUI thread — no subprocess
        result is needed and the call is non-blocking; failures are
        logged at debug level.
        """
        urls = {
            "camera": "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
            "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        }
        url = urls.get(target)
        if url is None:
            return
        try:
            import subprocess

            subprocess.Popen(
                ["open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.debug("open System Settings failed", exc_info=True)

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

        preset_idx = self._distraction_preset.currentIndex()
        preset_values = ["developer", "student", "writer", "custom"]
        distraction_preset = preset_values[preset_idx] if 0 <= preset_idx < len(preset_values) else "developer"
        custom_text = self._distraction_custom_domains.toPlainText() or ""
        custom_domains = [
            line.strip().lower()
            for line in custom_text.splitlines()
            if line.strip()
        ]

        # P0 §3.20: weekly schedule serialized as JSON-friendly dict.
        weekly_schedule: dict[str, list[str]] = {}
        for day in self._SCHEDULE_DAYS:
            combos = self._schedule_combos.get(day, [])
            weekly_schedule[day] = [c.currentText() for c in combos]

        # P0 §3.25: accessibility palette key.
        try:
            palette_value = self._palette_combo.itemData(
                self._palette_combo.currentIndex()
            ) or "default"
        except Exception:
            palette_value = "default"

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
            # P0 §3.10: focus protection (auto-armed distraction block)
            "enable_auto_distraction_block": self._auto_distraction_block.isChecked(),
            "auto_distraction_block_preset": distraction_preset,
            "auto_distraction_block_custom_domains": custom_domains,
            # P0 §3.12: OS-level notification routing
            "enable_os_notifications": self._os_notifications.isChecked(),
            # P0 §3.15: daily LLM budget cap (USD; 0 = unlimited).
            "daily_llm_budget_usd": float(self._budget_daily_spin.value()),
            # P0 §3.20: weekly schedule rules.
            "weekly_schedule": weekly_schedule,
            # P0 §3.25: color-blind palette variant.
            "palette_variant": str(palette_value),
        }

    def _apply_settings(self) -> None:
        # F04: serialise applies with a QMutex. ``tryLock`` returns False if
        # the previous apply is still in flight; in that case we ignore the
        # second click rather than emit a parallel ``settings_changed`` that
        # would race the first inside the daemon callback. The Apply button
        # is also disabled while the mutex is held so the user sees the UI
        # as busy.
        if not self._apply_mutex.tryLock():
            logger.debug(
                "Apply ignored — previous apply still in flight (coalesced)"
            )
            return
        if self._apply_btn is not None:
            try:
                self._apply_btn.setEnabled(False)
            except RuntimeError:
                pass
        try:
            self._settings_version += 1
            settings = self.get_settings()
            # Stamp the monotonic version so the daemon can discard a stale
            # apply that arrives after a newer one.
            settings["settings_version"] = self._settings_version
            self._persist_settings(settings)
            self.settings_changed.emit(settings)
            logger.info(
                "Settings applied: sensitivity=%s llm=%s version=%d",
                settings["sensitivity"],
                settings["llm_mode"],
                self._settings_version,
            )
        finally:
            self._apply_mutex.unlock()
            if self._apply_btn is not None:
                try:
                    self._apply_btn.setEnabled(True)
                except RuntimeError:
                    pass

    def _persist_settings(self, settings: dict) -> None:
        """Push every setting into QSettings and trigger a sync().

        P0 §3.20: weekly_schedule round-trips as JSON because QSettings'
        backing INI dialect cannot encode nested dicts portably.

        F53: sync() can fail silently (NoError but no actual on-disk
        write) on a read-only filesystem, in a sandbox container with a
        revoked ACL, or with the disk full. Prior code swallowed every
        exception and the user's "Apply" appeared to succeed even when
        nothing persisted. Now: catch exceptions explicitly, inspect
        ``QSettings.status()`` for the error class, and emit
        ``settings_save_failed(reason)`` so the controller can surface
        the failure.
        """
        import json as _json
        for key, value in settings.items():
            try:
                if isinstance(value, dict):
                    # QSettings dicts round-trip as JSON strings.
                    self._qs.setValue(key, _json.dumps(value, separators=(",", ":")))
                else:
                    self._qs.setValue(key, value)
            except Exception:
                # Per-key set failures are rare and per-key recoverable;
                # we continue setting the rest and let the sync() pass
                # be the canonical failure signal.
                logger.debug(
                    "Failed to set QSettings key %s", key, exc_info=True,
                )
        reason: str | None = None
        try:
            self._qs.sync()
        except Exception as exc:
            reason = f"sync raised: {exc!s}"
        else:
            # QSettings.sync() returns void; ``status()`` reports the
            # last error class. Anything other than NoError counts as a
            # failed save and must reach the user.
            try:
                status = self._qs.status()
                no_error = getattr(
                    QSettings.Status, "NoError",
                    getattr(QSettings, "NoError", 0),
                )
                if status != no_error:
                    reason = self._describe_qsettings_status(status)
            except Exception:
                # ``status()`` itself shouldn't raise, but if it does we
                # have no better signal than the absent sync exception.
                pass

        if reason is not None:
            logger.warning("QSettings sync failed: %s", reason)
            self.settings_save_failed.emit(reason)

    @staticmethod
    def _describe_qsettings_status(status: object) -> str:
        """Human-readable name for a QSettings.Status enum value."""
        try:
            access = getattr(QSettings.Status, "AccessError", None)
            fmt = getattr(QSettings.Status, "FormatError", None)
            if access is not None and status == access:
                return (
                    "access denied (read-only filesystem or sandbox ACL)"
                )
            if fmt is not None and status == fmt:
                return "settings file format invalid"
        except Exception:
            pass
        return f"QSettings status={status!r}"

    def _on_palette_changed(self, index: int) -> None:
        """P0 §3.25: persist + activate the chosen palette variant.

        We update the runtime palette immediately so future state-colour
        lookups respect the choice; existing widgets keep their pre-swap
        colour until next paint, hence the QMessageBox suggesting a
        restart for a fully consistent swap.
        """
        try:
            value = self._palette_combo.itemData(index) or "default"
        except Exception:
            value = "default"
        try:
            from cortex.apps.desktop_shell.palette_runtime import set_active_palette
            set_active_palette(str(value))
        except Exception:
            logger.debug("palette runtime swap failed", exc_info=True)
        try:
            self.palette_changed.emit(str(value))
        except Exception:
            pass
        if value != "default":
            try:
                QMessageBox.information(
                    self,
                    "Restart to apply",
                    "Restart Cortex to apply the new color palette everywhere.",
                )
            except Exception:
                logger.debug("palette restart dialog failed", exc_info=True)

    def _on_test_provider_clicked(self) -> None:
        """P0 §3.19: emit ``test_provider_requested`` carrying the
        provider key the user selected in the dropdown. The controller
        forwards to the daemon via the ``TEST_PROVIDER`` WS message;
        the daemon's reply lands in :meth:`apply_provider_test_result`.

        While the test is in flight the pill shows "Testing…" so the
        user knows the click registered.
        """
        llm_modes = ["bedrock", "vertex", "direct", "rule_based"]
        try:
            idx = self._llm_backend.currentIndex()
        except Exception:
            idx = 0
        provider = llm_modes[idx] if 0 <= idx < len(llm_modes) else "bedrock"
        try:
            self._provider_status_pill.setText("Testing…")
            self._provider_status_pill.setStyleSheet(
                "QLabel {"
                "  padding: 2px 10px;"
                f"  border-radius: {RADIUS_BUTTON}px;"
                f"  color: {_LABEL};"
                f"  background: {_GROUPED_BG};"
                "}"
            )
        except Exception:
            pass
        try:
            self.test_provider_requested.emit(provider)
        except Exception:
            logger.debug("test_provider_requested emit failed", exc_info=True)

    def apply_provider_test_result(self, payload: dict) -> None:
        """Render the result of a TEST_PROVIDER round-trip.

        Payload keys: ``provider`` (str), ``ok`` (bool),
        ``latency_ms`` (int|None), ``error`` (str|None). The pill flips
        to the result colour and quotes the latency (success) or error
        (failure).
        """
        if not isinstance(payload, dict):
            return
        provider = str(payload.get("provider") or "")
        ok = bool(payload.get("ok"))
        latency = payload.get("latency_ms")
        error = str(payload.get("error") or "")
        label_map = {
            "bedrock": "Bedrock",
            "vertex": "Vertex",
            "direct": "Anthropic",
            "rule_based": "Rule-based",
        }
        nice = label_map.get(provider, provider or "Provider")
        if ok:
            if isinstance(latency, (int, float)):
                text = f"{nice} ✓ {int(latency)} ms"
            else:
                text = f"{nice} ✓"
            color = SEMANTIC_LIGHT["success"]
        else:
            text = f"{nice} ✗ {error[:40]}" if error else f"{nice} ✗"
            color = SEMANTIC_LIGHT["danger"]
        try:
            self._provider_status_pill.setText(text)
            self._provider_status_pill.setStyleSheet(
                "QLabel {"
                "  padding: 2px 10px;"
                f"  border-radius: {RADIUS_BUTTON}px;"
                f"  color: {color};"
                f"  background: {_GROUPED_BG};"
                "}"
            )
        except Exception:
            logger.debug("provider pill update failed", exc_info=True)

    def apply_cost_response(self, payload: dict) -> None:
        """P0 §3.15: render the running daily spend in the Budget panel.

        Payload keys: ``cost_today`` (float USD), ``budget_today`` (float;
        ``budget_usd`` is honoured as a legacy alias). The controller
        calls this in response to the COST_RESPONSE WS message; the Phase
        4 follow-up wires the daemon side.
        """
        if not isinstance(payload, dict):
            return
        try:
            cost = float(payload.get("cost_today") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        # Accept the new ``budget_today`` wire key first; fall back to the
        # legacy ``budget_usd`` so older daemon builds still render.
        try:
            budget = float(
                payload.get("budget_today")
                or payload.get("budget_usd")
                or 0.0
            )
        except (TypeError, ValueError):
            budget = 0.0
        if budget > 0:
            line = f"Cost so far today: ${cost:.2f} of ${budget:.2f}"
        else:
            line = f"Cost so far today: ${cost:.2f}"
        try:
            self._budget_today_label.setText(line)
        except Exception:
            logger.debug("budget label update failed", exc_info=True)

    def _on_rotate_token(self) -> None:
        """Audit Debt-2 Commit 5: mint a fresh capability token, drop
        the old one, and emit ``auth_token_rotated`` so the controller
        forces a WS reconnect with the new value.

        Failures (filesystem read-only, etc.) update the inline status
        label instead of raising — the user explicitly asked for this
        action and deserves visible feedback either way.
        """
        from cortex.libs.auth import rotate_token
        from cortex.libs.logging.structured import EventType

        try:
            new_token = rotate_token()
        except Exception as exc:
            logger.exception("Token rotation failed")
            self._rotate_token_status.setText(
                f"Could not rotate token: {exc}"
            )
            return

        logger.info(
            "%s actor=user",
            EventType.AUTH_TOKEN_ROTATED.value,
        )
        # Briefly disable the button so a frustrated double-click does
        # not stack three rotations in 500 ms; re-enable after a beat.
        self._rotate_token_btn.setEnabled(False)
        self._rotate_token_status.setText(
            "Token rotated. Clients will reconnect within a few seconds."
        )
        self.auth_token_rotated.emit(new_token)

        # Re-enable after 1.5 s so the user can rotate again if they
        # have additional clients to invalidate.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, lambda: self._rotate_token_btn.setEnabled(True))

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
            # P0 §3.10 — focus protection controls.
            self._auto_distraction_block.setChecked(
                _get_bool("enable_auto_distraction_block", False),
            )
            preset = str(self._qs.value("auto_distraction_block_preset", "developer"))
            _preset_index = {
                "developer": 0, "student": 1, "writer": 2, "custom": 3,
            }
            if preset in _preset_index:
                self._distraction_preset.setCurrentIndex(_preset_index[preset])
            raw_domains = self._qs.value(
                "auto_distraction_block_custom_domains", [],
            )
            # Phase-3 P2-3: QSettings can round-trip an empty list as
            # ``None`` or a scalar string on some Qt builds — coerce to
            # ``list[str]`` defensively.
            if raw_domains is None:
                domains_list: list[str] = []
            elif isinstance(raw_domains, str):
                domains_list = [raw_domains] if raw_domains.strip() else []
            elif isinstance(raw_domains, (list, tuple)):
                domains_list = [
                    str(d).strip()
                    for d in raw_domains
                    if isinstance(d, (str,)) and str(d).strip()
                ]
            else:
                domains_list = []
            self._distraction_custom_domains.setPlainText(
                "\n".join(domains_list),
            )
            # P0 §3.12 — OS notifications.
            self._os_notifications.setChecked(
                _get_bool("enable_os_notifications", True),
            )
            # P0 §3.15 — daily LLM budget cap.
            try:
                budget_raw = self._qs.value("daily_llm_budget_usd", 1.50)
                self._budget_daily_spin.setValue(float(budget_raw))
            except (TypeError, ValueError):
                self._budget_daily_spin.setValue(1.50)
            # P0 §3.20 — weekly schedule.
            try:
                import json as _json
                schedule_raw = self._qs.value("weekly_schedule", "")
                if isinstance(schedule_raw, str) and schedule_raw:
                    schedule_dict = _json.loads(schedule_raw)
                elif isinstance(schedule_raw, dict):
                    schedule_dict = schedule_raw
                else:
                    schedule_dict = {}
                for day, combos in self._schedule_combos.items():
                    saved = schedule_dict.get(day, []) if isinstance(schedule_dict, dict) else []
                    for idx, cb in enumerate(combos):
                        if idx < len(saved):
                            val = str(saved[idx])
                            cb_idx = cb.findText(val) if hasattr(cb, "findText") else -1
                            if cb_idx >= 0:
                                cb.setCurrentIndex(cb_idx)
            except Exception:
                logger.debug("weekly schedule restore failed", exc_info=True)
            # P0 §3.25 — palette variant.
            try:
                palette_value = str(self._qs.value("palette_variant", "default"))
                for i in range(self._palette_combo.count()):
                    if self._palette_combo.itemData(i) == palette_value:
                        self._palette_combo.setCurrentIndex(i)
                        break
                # Apply at startup so the runtime helper returns the
                # right palette from the very first state lookup.
                from cortex.apps.desktop_shell.palette_runtime import (
                    set_active_palette,
                )
                set_active_palette(palette_value)
            except Exception:
                logger.debug("palette restore failed", exc_info=True)
        except Exception:
            logger.debug("Failed to restore persisted settings", exc_info=True)

    def apply_payload(self, payload: dict) -> None:
        """Apply a ``SETTINGS_SYNC`` payload arriving from the daemon.

        Phase-3 P0-2 / Audit-1.5 P0-2: previously only the legacy
        quiet_mode / sensitivity / webcam / interventions keys were
        echoed back; the new P0 fields (focus protection toggle, preset,
        custom domains, OS notifications) silently drifted between the
        local UI and the daemon's authoritative state. Now every
        post-P0 field round-trips so a second app instance / another
        surface flipping a toggle flows back into the dashboard
        widgets without requiring the user to click Apply locally.
        """
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
            # P0 §3.10 — Focus protection controls.
            if "enable_auto_distraction_block" in payload:
                self._auto_distraction_block.setChecked(
                    bool(payload["enable_auto_distraction_block"]),
                )
            if "auto_distraction_block_preset" in payload:
                preset = str(payload["auto_distraction_block_preset"] or "")
                _preset_index = {
                    "developer": 0,
                    "student": 1,
                    "writer": 2,
                    "custom": 3,
                }
                if preset in _preset_index:
                    self._distraction_preset.setCurrentIndex(_preset_index[preset])
            if "auto_distraction_block_custom_domains" in payload:
                raw = payload["auto_distraction_block_custom_domains"]
                if isinstance(raw, list):
                    cleaned = [
                        str(d).strip().lower()
                        for d in raw
                        if isinstance(d, str) and d.strip()
                    ]
                    self._distraction_custom_domains.setPlainText(
                        "\n".join(cleaned),
                    )
            # P0 §3.12 — OS notifications toggle.
            if "enable_os_notifications" in payload:
                self._os_notifications.setChecked(
                    bool(payload["enable_os_notifications"]),
                )
        except Exception:
            logger.debug("Failed to apply SETTINGS_SYNC payload", exc_info=True)
        self._persist_settings(self.get_settings())
