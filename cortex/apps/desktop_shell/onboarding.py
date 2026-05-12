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
    CX_BORDER_DEFAULT,
    CX_FONT_BRAND,
    CX_FONT_SANS,
    CX_SUCCESS,
    CX_SUCCESS_DIM,
    CX_SURFACE,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    RADIUS_SM,
    SP2,
    SP3,
    SP4,
    SP5,
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
    """Trigger the macOS camera-permission prompt.

    E.3: previously this just opened System Settings, leaving the user
    to drill into Privacy → Camera themselves. Use the proper
    AVFoundation request via
    :func:`cortex.libs.utils.platform.request_camera_permission`, which
    fires the native dialog. Fall back to System Settings only if the
    AVFoundation framework is unavailable (CI / Linux test harnesses).
    """
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
    extensions_requested = Signal()  # E.5: from the new step-4 button

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

        # Step 4: Extensions (E.5: actionable button — previously the wizard
        # only displayed a hint with no way to act on it).
        ext_frame = self._make_section("4", "Connect Extensions")
        ext_layout = ext_frame.layout()
        hint = QLabel(
            "Install the browser and editor extensions to give Cortex "
            "context about your tabs and code. You can also do this later "
            "from the tray menu → Connect Extensions."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        ext_layout.addWidget(hint)

        connect_btn = QPushButton("Open Connections")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.setFixedHeight(36)
        connect_btn.setStyleSheet(BTN_ACCENT_QSS)
        connect_btn.clicked.connect(self.extensions_requested.emit)
        ext_layout.addWidget(connect_btn)

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
        frame = self._make_section("3", "AWS Bedrock Bearer Token")
        layout = frame.layout()

        desc = QLabel(
            "Cortex uses Anthropic Claude via AWS Bedrock. Paste your "
            "long-lived bearer token below \u2014 it's stored only in the "
            "macOS Keychain and never written to disk."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(desc)

        config = get_config()

        # Region picker (defaults to the user's configured region).
        region_combo = QComboBox()
        region_combo.addItems(["us-east-2", "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2"])
        region_combo.setCurrentText(config.llm.bedrock.aws_region)
        region_combo.setStyleSheet(f"""
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
        self._region_combo = region_combo
        layout.addWidget(region_combo)

        # Bearer token input
        key_row = QHBoxLayout()
        key_row.setSpacing(SP2)
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("AWS Bedrock bearer token")
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

        # Check if token already in Keychain
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
            saved_label.setStyleSheet(
                f"font-family: {CX_FONT_SANS}; font-size: 12px; "
                f"color: {CX_SUCCESS}; border: none;"
            )
            layout.addWidget(saved_label)

        hint = QLabel(
            "Cortex calls Claude via AWS Bedrock inference profiles  \u00b7  "
            "Token stored in macOS Keychain (service: cortex.bedrock)  \u00b7  "
            "Without a token, the daemon falls back to rule-based plans."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; "
            f"color: {CX_TEXT_TERTIARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(hint)

        return frame

    def _save_api_key(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Error", "Please paste a Bedrock bearer token.")
            return
        try:
            import keyring
            config = get_config()
            keyring.set_password(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
                key,
            )
            # Persist the chosen region back to config (env var override
            # is recomputed on next get_config()).
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
