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

from PySide6.QtCore import QMutex, QSettings, Qt, Signal
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

        chain_tab_order(
            back_btn,
            self._webcam_enabled,
            self._input_telemetry_enabled,
            self._interventions_enabled,
            self._sensitivity_slider,
            self._cooldown_spin,
            self._quiet_mode,
            self._quiet_duration,
            self._llm_backend,
            self._debug_capture,
            self._debug_rppg,
            self._debug_state,
            self._debug_llm,
            close_btn,
            apply_btn,
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

        F53: sync() can fail silently (NoError but no actual on-disk
        write) on a read-only filesystem, in a sandbox container with a
        revoked ACL, or with the disk full. Prior code swallowed every
        exception and the user's "Apply" appeared to succeed even when
        nothing persisted. Now: catch exceptions explicitly, inspect
        ``QSettings.status()`` for the error class, and emit
        ``settings_save_failed(reason)`` so the controller can surface
        the failure.
        """
        for key, value in settings.items():
            try:
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
