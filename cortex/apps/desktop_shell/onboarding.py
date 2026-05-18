"""Desktop shell onboarding — 4-step first-run wizard (macOS-native refactor).

Visual layer adopts:

* Native popover vibrancy material under the window (via mac_native)
* Horizontal progress strip showing all 4 steps at once
* Sentence-case section headings, SF system fonts
* Terracotta number badges + Cormorant Garamond brand wordmark preserved
* Native ``AVCaptureDevice.requestAccessForMediaType_`` for camera grant
  (already in cortex/libs/utils/platform.py) and the standard
  ``AXIsProcessTrustedWithOptions`` for accessibility

Public API (Signals + ``onboarding_marker_path``) preserved byte-identical.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_DIM,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_REGULAR,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
    SP8,
)
from cortex.libs.config.settings import get_config

_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = "#5C5854"
_LABEL_TERTIARY = "#827971"
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_SUCCESS = SEMANTIC_LIGHT["success"]
_SUCCESS_DIM = "rgba(48, 178, 87, 0.10)"


# ---------------------------------------------------------------------------
# Permission checks (unchanged — keep the AVFoundation + AX paths)
# ---------------------------------------------------------------------------

def check_camera_permission() -> bool:
    try:
        from cortex.libs.utils import check_camera_permission as _check
        return _check()
    except Exception:
        return False


def check_accessibility_permission() -> bool:
    try:
        from cortex.libs.utils import check_accessibility_permission as _check
        return _check()
    except Exception:
        return False


def request_camera_permission() -> None:
    """Trigger the native AVFoundation camera permission dialog."""
    try:
        from cortex.libs.utils.platform import (
            request_camera_permission as _request_camera_permission,
        )

        _request_camera_permission()
        return
    except Exception:
        pass
    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
        ])
    except Exception:
        pass


def request_accessibility_permission() -> None:
    try:
        import ApplicationServices  # type: ignore[import-not-found]
        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        ApplicationServices.AXIsProcessTrustedWithOptions(options)
    except Exception:
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Native progress strip — 4 dots connected by hairlines
# ---------------------------------------------------------------------------

class _ProgressStrip(QWidget):
    """Horizontal step indicator: 4 numbered dots, the current one
    rendered as the terracotta brand accent."""

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = count
        self._current = 0
        self._dots: list[QLabel] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for i in range(count):
            dot = QLabel(str(i + 1))
            dot.setFixedSize(22, 22)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            self._dots.append(dot)
            layout.addWidget(dot)
            if i < count - 1:
                bar = QFrame()
                bar.setFixedHeight(1)
                bar.setMinimumWidth(20)
                bar.setStyleSheet(f"background: {_SEPARATOR};")
                layout.addWidget(bar, stretch=1)
        self._restyle()

    def set_current(self, index: int) -> None:
        self._current = max(0, min(index, self._count - 1))
        self._restyle()

    def _restyle(self) -> None:
        for i, dot in enumerate(self._dots):
            if i == self._current:
                dot.setStyleSheet(
                    f"background: {BRAND_ACCENT};"
                    f" color: #FFF; border-radius: 11px;"
                )
            elif i < self._current:
                dot.setStyleSheet(
                    f"background: {BRAND_ACCENT_DIM};"
                    f" color: {BRAND_ACCENT}; border-radius: 11px;"
                )
            else:
                dot.setStyleSheet(
                    f"background: {_GROUPED_BG};"
                    f" color: {_LABEL_TERTIARY}; border-radius: 11px;"
                )


# ---------------------------------------------------------------------------
# OnboardingWindow
# ---------------------------------------------------------------------------

class OnboardingWindow(QWidget):
    """Four-step first-run setup. Public Signals unchanged."""

    completed = Signal()
    open_settings_requested = Signal()
    run_calibration_requested = Signal()
    extensions_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex Setup")
        # Roomy default. The previous 560×620 was shorter than the
        # combined card heights, so Qt compressed widgets into each
        # other (e.g. card 3's region picker + token row overlapped the
        # description and hint paragraphs).
        self.setMinimumSize(600, 720)
        self.resize(640, 820)
        self.setStyleSheet(f"background: {_WINDOW_BG}; color: {_LABEL};")
        self._build_ui()

        # Permissions are granted in System Settings out-of-process — there's
        # no callback path back into the app. Poll every 1.5s while the
        # wizard is visible so the "Not granted" pills flip to "Granted"
        # without a relaunch. Timer is paused on hide via showEvent below.
        self._permission_timer = QTimer(self)
        self._permission_timer.setInterval(1500)
        self._permission_timer.timeout.connect(self._refresh_permission_states)
        self._permission_timer.start()

    def _refresh_permission_states(self) -> None:
        try:
            cam = check_camera_permission()
            getattr(self._camera_step, "_cortex_set_state", lambda _b: None)(cam)
        except Exception:
            pass
        try:
            acc = check_accessibility_permission()
            getattr(self._accessibility_step, "_cortex_set_state", lambda _b: None)(acc)
        except Exception:
            pass

    # -- Native chrome ---------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        # First-show centering — prevents stale Qt geometry from a previous
        # multi-monitor session from stranding the window off-screen.
        if not getattr(self, "_positioned_once", False):
            try:
                screen = self.screen()
                if screen is not None:
                    geo = screen.availableGeometry()
                    self.move(
                        geo.x() + (geo.width() - self.width()) // 2,
                        geo.y() + max(40, (geo.height() - self.height()) // 4),
                    )
            except Exception:
                pass
            self._positioned_once = True
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="popover")
        except Exception:
            pass
        # Resume permission polling whenever the wizard becomes visible.
        try:
            if not self._permission_timer.isActive():
                self._permission_timer.start()
            self._refresh_permission_states()
        except Exception:
            pass

    def hideEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        try:
            self._permission_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def _build_ui(self) -> None:
        # Two-tier layout:
        #   outer: window itself (no margin) → host QScrollArea
        #   inner: scrollable content widget with the actual layout
        # This way the user can shrink the window without overlapping
        # any card, and tall content scrolls naturally.
        try:
            from PySide6.QtWidgets import QScrollArea
        except ImportError:  # pragma: no cover
            QScrollArea = None  # type: ignore[assignment]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        content.setObjectName("CortexOnboardingContent")
        content.setStyleSheet(
            f"#CortexOnboardingContent {{ background: {_WINDOW_BG}; }}"
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SP8, SP8, SP8, SP8)
        layout.setSpacing(SP5)

        if QScrollArea is not None:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet(
                "QScrollArea { border: none; background: transparent; }"
                "QScrollBar:vertical { background: transparent; width: 8px; }"
                "QScrollBar::handle:vertical {"
                "  background: rgba(0,0,0,0.18); border-radius: 4px;"
                "  min-height: 24px;"
                "}"
                "QScrollBar::handle:vertical:hover { background: rgba(0,0,0,0.32); }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
                "  height: 0;"
                "}"
            )
            scroll.setWidget(content)
            outer.addWidget(scroll)
        else:  # pragma: no cover - test stub path
            outer.addWidget(content)

        # ── Brand wordmark + welcome header ───────────────────────────
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-style: italic; font-size: {FS_BODY}px;"
            f"font-weight: {FW_REGULAR};"
            f"color: {BRAND_ACCENT}; background: transparent;"
        )
        layout.addWidget(brand)
        layout.addSpacing(SP2)

        title = QLabel("Welcome to Cortex")
        title.setFont(mac_native.system_font(FS_TITLE, "bold"))
        title.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Grant permissions, choose your LLM backend, and connect your "
            "browser and editor. This only takes a minute."
        )
        subtitle.setWordWrap(True)
        subtitle.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        subtitle.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP3)

        # ── Progress strip ────────────────────────────────────────────
        self._progress = _ProgressStrip(4)
        layout.addWidget(self._progress)
        layout.addSpacing(SP3)

        # ── Step 1: Camera ────────────────────────────────────────────
        self._camera_step = self._make_step(
            "Camera access",
            "Required for biometric sensing via webcam.",
            check_camera_permission(),
            "Grant Access",
            request_camera_permission,
            "1",
        )
        layout.addWidget(self._camera_step)

        # ── Step 2: Accessibility ─────────────────────────────────────
        self._accessibility_step = self._make_step(
            "Accessibility",
            "Required for keyboard and mouse tracking.",
            check_accessibility_permission(),
            "Grant Access",
            request_accessibility_permission,
            "2",
        )
        layout.addWidget(self._accessibility_step)

        # ── Step 3: LLM backend ───────────────────────────────────────
        layout.addWidget(self._make_llm_step())

        # ── Step 4: Connect Extensions ────────────────────────────────
        ext_frame = self._make_section("4", "Connect extensions")
        ext_layout = ext_frame.layout()
        hint = QLabel(
            "Install the browser and editor extensions to give Cortex "
            "context about your tabs and code. You can also do this "
            "later from the menu bar → Connect Extensions."
        )
        hint.setWordWrap(True)
        hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        hint.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none;"
        )
        ext_layout.addWidget(hint)

        connect_btn = QPushButton("Open Connections")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.setMinimumHeight(34)
        connect_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        connect_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        connect_btn.clicked.connect(self.extensions_requested.emit)
        ext_layout.addWidget(connect_btn)
        layout.addWidget(ext_frame)

        layout.addStretch()

        # ── Finish bar ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        finish_btn = QPushButton("Get Started")
        finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        finish_btn.setMinimumHeight(38)
        finish_btn.setMinimumWidth(140)
        finish_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        finish_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 8px 24px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {_LABEL};"
            "  color: #FFF; border: none;"
            "}"
            "QPushButton:hover { background: #333; }"
        )
        finish_btn.clicked.connect(self.completed.emit)
        btn_row.addWidget(finish_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Section helpers
    # ------------------------------------------------------------------

    def _make_section(self, number: str, title: str) -> QFrame:
        frame = QFrame()
        # Scope to objectName so the QFrame stylesheet (background +
        # 0.5px hairline + 8px radius) doesn't cascade onto every
        # QLabel/QPushButton descendant (those classes inherit QFrame
        # in Qt and would otherwise pick up the white background +
        # border, scrambling text rendering).
        frame.setObjectName("CortexOnbStep")
        frame.setStyleSheet(
            "QFrame#CortexOnbStep {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        header = QHBoxLayout()
        header.setSpacing(SP3)

        num_label = QLabel(number)
        num_label.setFixedSize(22, 22)
        num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        num_label.setStyleSheet(
            f"color: {BRAND_ACCENT}; background: {BRAND_ACCENT_DIM};"
            f" border: none; border-radius: 11px;"
        )
        header.addWidget(num_label)

        heading = QLabel(title)
        heading.setFont(mac_native.system_font(FS_BODY, "semibold"))
        heading.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        header.addWidget(heading)
        header.addStretch()
        layout.addLayout(header)
        return frame

    def _make_step(
        self,
        title: str,
        description: str,
        granted: bool,
        btn_text: str,
        action: object,
        number: str,
    ) -> QFrame:
        """Build a permission step.

        The status pill + Grant button are kept as attributes on the frame
        so the polling timer (``_refresh_permission_states``) can flip them
        when the user grants the underlying OS permission without forcing
        the user to relaunch the wizard. This addresses the bug where
        granting Accessibility in System Settings didn't update the
        onboarding "Not granted" pill.
        """
        frame = self._make_section(number, title)
        layout = frame.layout()

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none;"
        )
        layout.addWidget(desc)

        row = QHBoxLayout()

        status = QLabel("")
        status.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        row.addWidget(status)
        row.addStretch()

        btn = QPushButton(btn_text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(28)
        btn.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        btn.setStyleSheet(
            "QPushButton {"
            "  padding: 4px 12px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        if callable(action):
            btn.clicked.connect(action)
        row.addWidget(btn)

        layout.addLayout(row)

        def _set_state(is_granted: bool) -> None:
            if is_granted:
                status.setText("Granted")
                status.setStyleSheet(
                    f"color: {_SUCCESS}; background: {_SUCCESS_DIM};"
                    f" border: none; border-radius: {RADIUS_BUTTON}px;"
                    "  padding: 3px 8px;"
                )
                btn.setVisible(False)
            else:
                status.setText("Not granted")
                status.setStyleSheet(
                    f"color: {_LABEL_TERTIARY}; background: rgba(0,0,0,0.04);"
                    f" border: none; border-radius: {RADIUS_BUTTON}px;"
                    "  padding: 3px 8px;"
                )
                btn.setVisible(True)

        _set_state(bool(granted))
        # Stash the refresh closure on the frame so the polling timer can
        # call it without re-resolving widgets by index.
        frame._cortex_set_state = _set_state  # type: ignore[attr-defined]
        return frame

    def _make_llm_step(self) -> QFrame:
        frame = self._make_section("3", "AWS Bedrock bearer token")
        layout = frame.layout()

        desc = QLabel(
            "Cortex calls Anthropic Claude via AWS Bedrock. Paste your "
            "long-lived bearer token below — it's stored only in the macOS "
            "Keychain and never written to disk."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(f"color: {_LABEL_SECONDARY}; border: none;")
        layout.addWidget(desc)

        config = get_config()

        region_combo = QComboBox()
        region_combo.addItems([
            "us-east-2", "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2",
        ])
        region_combo.setCurrentText(config.llm.bedrock.aws_region)
        region_combo.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        # Explicit min-height so the combo doesn't get squashed when Qt
        # tries to fit a too-tall card into a too-short window.
        region_combo.setMinimumHeight(30)
        region_combo.setStyleSheet(
            "QComboBox {"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 12px;"
            "}"
        )
        self._region_combo = region_combo
        layout.addWidget(region_combo)

        key_row = QHBoxLayout()
        key_row.setSpacing(SP2)
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("AWS Bedrock bearer token")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._key_input.setMinimumHeight(32)
        self._key_input.setStyleSheet(
            "QLineEdit {"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 12px;"
            "}"
            f"QLineEdit:focus {{ border: 1.5px solid {BRAND_ACCENT}; }}"
        )
        key_row.addWidget(self._key_input)

        save_key_btn = QPushButton("Save")
        save_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_key_btn.setMinimumHeight(32)
        save_key_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        save_key_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        save_key_btn.clicked.connect(self._save_api_key)
        key_row.addWidget(save_key_btn)

        self._key_widget = QWidget()
        self._key_widget.setLayout(key_row)
        layout.addWidget(self._key_widget)

        has_key = False
        try:
            import keyring
            existing = keyring.get_password(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
            )
            has_key = bool(existing)
        except Exception:
            pass

        if has_key:
            saved_label = QLabel("Bedrock bearer token found in Keychain")
            saved_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            saved_label.setStyleSheet(
                f"color: {_SUCCESS}; border: none;"
            )
            layout.addWidget(saved_label)

        hint = QLabel(
            "Cortex calls Claude via AWS Bedrock inference profiles  ·  "
            "Stored in macOS Keychain (service: cortex.bedrock)  ·  "
            "Without a token, the daemon falls back to rule-based plans."
        )
        hint.setWordWrap(True)
        hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        hint.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; border: none;"
        )
        layout.addWidget(hint)

        return frame

    def _save_api_key(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Error", "Please paste a Bedrock bearer token.")
            return
        # Bedrock bearer tokens are JWT-shaped and run 100+ chars; anything under
        # 20 is almost certainly a paste error (e.g. truncated copy, AWS account
        # ID, profile name). Catch this before we write garbage to the Keychain.
        if len(key) < 20:
            QMessageBox.warning(
                self,
                "Token looks too short",
                "Token looks too short — Bedrock tokens are typically 100+ chars.",
            )
            return
        try:
            import keyring
            config = get_config()
            keyring.set_password(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
                key,
            )
            try:
                config.llm.bedrock.aws_region = self._region_combo.currentText()
            except AttributeError:
                pass
            QMessageBox.information(
                self,
                "Saved",
                "Bedrock bearer token saved to macOS Keychain. Restart "
                "Cortex (or sign out and back in) to pick it up.",
            )
            self._key_input.clear()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save token:\n{e}")


def onboarding_marker_path() -> Path:
    return Path(get_config().storage.path) / ".onboarding_complete"
