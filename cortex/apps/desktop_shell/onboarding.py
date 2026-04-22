"""
Desktop shell onboarding — 4-step first-run wizard for DMG installs.

Steps:
  1. Camera permission
  2. Accessibility / Input Monitoring permission
  3. LLM backend (Azure key via Keychain, Ollama, or rule-based)
  4. Connect extensions (browser + editor)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
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

from cortex.apps.desktop_shell.tokens import (
    BTN_ACCENT_QSS,
    BTN_PRIMARY_QSS,
    CARD_QSS,
    CX_ACCENT,
    CX_ACCENT_DIM,
    CX_BG,
    CX_BORDER,
    CX_BORDER_DEFAULT,
    CX_FONT_BRAND,
    CX_FONT_SANS,
    CX_SUCCESS,
    CX_SUCCESS_DIM,
    CX_SURFACE,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    PAGE_TITLE_QSS,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_SM,
    RADIUS_FULL,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
    SP8,
    SP10,
)
from cortex.libs.config.settings import get_config


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------

def check_camera_permission() -> bool:
    """Return True if camera permission is granted (best-effort)."""
    try:
        from cortex.libs.utils import check_camera_permission as _check
        return _check()
    except Exception:
        return False


def check_accessibility_permission() -> bool:
    """Return True if accessibility permission is granted."""
    try:
        from cortex.libs.utils import check_accessibility_permission as _check
        return _check()
    except Exception:
        return False


def request_camera_permission() -> None:
    """Open System Settings to the Camera privacy pane."""
    try:
        subprocess.Popen([
            "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
        ])
    except Exception:
        pass


def request_accessibility_permission() -> None:
    """Trigger the native macOS Accessibility permission dialog via pyobjc."""
    try:
        import ApplicationServices
        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        ApplicationServices.AXIsProcessTrustedWithOptions(options)
    except Exception:
        # Fallback: open System Settings manually
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Onboarding Window
# ---------------------------------------------------------------------------

class OnboardingWindow(QWidget):
    """Four-step first-run setup for packaged DMG installs."""

    completed = Signal()
    open_settings_requested = Signal()
    run_calibration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex Setup")
        self.setMinimumSize(520, 520)
        self.setStyleSheet(f"background: {CX_BG};")
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP10, SP8, SP10, SP8)
        layout.setSpacing(SP5)

        # ── Welcome header ───────────────────────────────────────────
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {CX_FONT_BRAND}; "
            f"font-style: italic; font-size: 16px; font-weight: 400; "
            f"color: {CX_ACCENT}; background: transparent;"
        )
        layout.addWidget(brand)
        layout.addSpacing(SP2)

        title = QLabel("Welcome to Cortex")
        title.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 24px; "
            f"font-weight: 700; color: {CX_TEXT}; background: transparent;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "Grant permissions, choose your LLM backend, and connect "
            "your browser and editor. This only takes a minute."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; "
            f"color: {CX_TEXT_SECONDARY}; background: transparent; "
            f"line-height: 1.5;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP3)

        # Step 1: Camera
        layout.addWidget(self._make_step(
            "Camera Access",
            "Required for biometric sensing via webcam.",
            "Granted" if check_camera_permission() else "Not granted",
            check_camera_permission(),
            "Grant Access",
            request_camera_permission,
            "1",
        ))

        # Step 2: Accessibility
        layout.addWidget(self._make_step(
            "Accessibility",
            "Required for keyboard and mouse tracking.",
            "Granted" if check_accessibility_permission() else "Not granted",
            check_accessibility_permission(),
            "Grant Access",
            request_accessibility_permission,
            "2",
        ))

        # Step 3: LLM backend
        layout.addWidget(self._make_llm_step())

        # Step 4: Extensions
        ext_frame = self._make_section("4", "Connect Extensions")
        ext_layout = ext_frame.layout()
        hint = QLabel(
            "You can connect your browser and editor now, or later "
            "from the Connections panel."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        ext_layout.addWidget(hint)
        layout.addWidget(ext_frame)

        # ── Finish button ────────────────────────────────────────────
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        finish_btn = QPushButton("Get Started")
        finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        finish_btn.setFixedHeight(40)
        finish_btn.setMinimumWidth(140)
        finish_btn.setStyleSheet(BTN_PRIMARY_QSS)
        finish_btn.clicked.connect(self.completed.emit)
        btn_row.addWidget(finish_btn)
        layout.addLayout(btn_row)

    def _make_section(self, number: str, title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"QFrame {{ {CARD_QSS} }}")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        header = QHBoxLayout()
        header.setSpacing(SP3)

        num_label = QLabel(number)
        num_label.setFixedSize(24, 24)
        num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; font-weight: 600; "
            f"color: {CX_ACCENT}; background: {CX_ACCENT_DIM}; "
            f"border: none; border-radius: 12px;"
        )
        header.addWidget(num_label)

        heading = QLabel(title)
        heading.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 14px; "
            f"font-weight: 600; color: {CX_TEXT}; border: none;"
        )
        header.addWidget(heading)
        header.addStretch()
        layout.addLayout(header)

        return frame

    def _make_step(
        self,
        title: str,
        description: str,
        status_text: str,
        granted: bool,
        btn_text: str,
        action: object,
        number: str,
    ) -> QFrame:
        frame = self._make_section(number, title)
        layout = frame.layout()

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(desc)

        row = QHBoxLayout()

        if granted:
            status_color = CX_SUCCESS
            status_bg = CX_SUCCESS_DIM
        else:
            status_color = CX_TEXT_TERTIARY
            status_bg = "rgba(0,0,0,0.04)"

        status = QLabel(status_text)
        status.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; font-weight: 500; "
            f"color: {status_color}; background: {status_bg}; "
            f"border: none; border-radius: {RADIUS_SM}px; "
            f"padding: 3px 8px;"
        )
        row.addWidget(status)
        row.addStretch()

        if not granted:
            btn = QPushButton(btn_text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.setStyleSheet(BTN_ACCENT_QSS)
            btn.clicked.connect(action)
            row.addWidget(btn)

        layout.addLayout(row)
        return frame

    def _make_llm_step(self) -> QFrame:
        frame = self._make_section("3", "LLM Backend")
        layout = frame.layout()

        desc = QLabel("Choose how Cortex generates intervention content.")
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(desc)

        config = get_config()
        current_mode = config.llm.mode

        mode_combo = QComboBox()
        mode_combo.addItems(["azure", "local", "rule_based"])
        mode_combo.setCurrentText(current_mode)
        mode_combo.setStyleSheet(f"""
            QComboBox {{
                font-family: {CX_FONT_SANS};
                font-size: 13px;
                color: {CX_TEXT};
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_SM}px;
                padding: 8px 12px;
            }}
        """)
        layout.addWidget(mode_combo)

        # API key input (shown for Azure)
        key_row = QHBoxLayout()
        key_row.setSpacing(SP2)
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("Azure OpenAI API key")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                font-family: {CX_FONT_SANS};
                font-size: 13px;
                color: {CX_TEXT};
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_SM}px;
                padding: 8px 12px;
            }}
            QLineEdit:focus {{
                border: 1.5px solid {CX_ACCENT};
            }}
        """)
        key_row.addWidget(self._key_input)

        save_key_btn = QPushButton("Save")
        save_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_key_btn.setFixedHeight(36)
        save_key_btn.setStyleSheet(BTN_ACCENT_QSS)
        save_key_btn.clicked.connect(self._save_api_key)
        key_row.addWidget(save_key_btn)

        self._key_widget = QWidget()
        self._key_widget.setLayout(key_row)
        layout.addWidget(self._key_widget)

        # Check if key already in Keychain
        has_key = False
        try:
            import keyring
            existing = keyring.get_password(
                config.llm.azure.keychain_service,
                config.llm.azure.keychain_account,
            )
            has_key = bool(existing)
        except Exception:
            pass

        if has_key:
            saved_label = QLabel("API key found in Keychain")
            saved_label.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 12px; "
                f"color: {CX_SUCCESS}; border: none;"
            )
            layout.addWidget(saved_label)

        hint = QLabel(
            "Azure: API key stored in macOS Keychain  \u00b7  "
            "Local: Requires Ollama  \u00b7  "
            "Rule-based: Offline mode"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; "
            f"color: {CX_TEXT_TERTIARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(hint)

        # Toggle key input visibility
        def _on_mode_change(mode: str) -> None:
            self._key_widget.setVisible(mode == "azure")
        mode_combo.currentTextChanged.connect(_on_mode_change)
        _on_mode_change(current_mode)

        return frame

    def _save_api_key(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Error", "Please enter an API key.")
            return
        try:
            import keyring
            config = get_config()
            keyring.set_password(
                config.llm.azure.keychain_service,
                config.llm.azure.keychain_account,
                key,
            )
            QMessageBox.information(self, "Saved", "API key saved to macOS Keychain.")
            self._key_input.clear()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save key:\n{e}")


def onboarding_marker_path() -> Path:
    return Path(get_config().storage.path) / ".onboarding_complete"
