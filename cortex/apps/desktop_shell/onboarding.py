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
    CX_ACCENT,
    CX_BG,
    CX_BORDER_DEFAULT,
    CX_SURFACE,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    RADIUS_MD,
    SP4,
    SP5,
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
        self.setMinimumSize(520, 480)
        self.setStyleSheet(f"background: {CX_BG};")
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP5 * 2, SP5 * 2, SP5 * 2, SP5 * 2)
        layout.setSpacing(SP4)

        title = QLabel("Set Up Cortex")
        title.setStyleSheet(
            f"font-family: Georgia, serif; font-size: 28px; "
            f"font-weight: 700; color: {CX_TEXT};"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "Grant permissions, choose your LLM backend, and connect "
            "your browser and editor. This only takes a minute."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {CX_TEXT_SECONDARY}; font-size: 13px;")
        layout.addWidget(subtitle)

        # Step 1: Camera
        layout.addWidget(self._make_step(
            "1. Camera",
            "Granted" if check_camera_permission() else "Not granted",
            check_camera_permission(),
            "Grant Access",
            request_camera_permission,
        ))

        # Step 2: Accessibility
        layout.addWidget(self._make_step(
            "2. Accessibility",
            "Granted" if check_accessibility_permission() else "Not granted",
            check_accessibility_permission(),
            "Grant Access",
            request_accessibility_permission,
        ))

        # Step 3: LLM backend
        layout.addWidget(self._make_llm_step())

        # Step 4: Extensions
        ext_frame = self._make_section("4. Connect Extensions")
        ext_layout = ext_frame.layout()
        hint = QLabel(
            "You can connect your browser and editor now, or later "
            "from the Connections panel in the tray menu."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 13px; color: {CX_TEXT_SECONDARY};")
        ext_layout.addWidget(hint)
        layout.addWidget(ext_frame)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        finish_btn = QPushButton("Finish Setup")
        finish_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 10px 24px; border-radius: 9999px;
                background: {CX_TEXT}; color: white;
                font-size: 14px; font-weight: 500; border: none;
            }}
            QPushButton:hover {{ background: #333; }}
        """)
        finish_btn.clicked.connect(self.completed.emit)
        btn_row.addWidget(finish_btn)
        layout.addLayout(btn_row)
        layout.addStretch()

    def _make_section(self, title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_MD}px;
            }}
        """)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        heading = QLabel(title)
        heading.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {CX_TEXT}; border: none;"
        )
        layout.addWidget(heading)
        return frame

    def _make_step(
        self,
        title: str,
        status_text: str,
        granted: bool,
        btn_text: str,
        action: object,
    ) -> QFrame:
        frame = self._make_section(title)
        layout = frame.layout()

        row = QHBoxLayout()
        status = QLabel(status_text)
        color = CX_ACCENT if granted else CX_TEXT_TERTIARY
        status.setStyleSheet(f"font-size: 13px; color: {color}; border: none;")
        row.addWidget(status)
        row.addStretch()

        if not granted:
            btn = QPushButton(btn_text)
            btn.setStyleSheet(f"""
                QPushButton {{
                    padding: 6px 14px; border-radius: 9999px;
                    background: {CX_ACCENT}; color: white;
                    font-size: 12px; font-weight: 500; border: none;
                }}
                QPushButton:hover {{ background: #C46547; }}
            """)
            btn.clicked.connect(action)
            row.addWidget(btn)

        layout.addLayout(row)
        return frame

    def _make_llm_step(self) -> QFrame:
        frame = self._make_section("3. LLM Backend")
        layout = frame.layout()

        config = get_config()
        current_mode = config.llm.mode

        mode_combo = QComboBox()
        mode_combo.addItems(["azure", "local", "rule_based"])
        mode_combo.setCurrentText(current_mode)
        mode_combo.setStyleSheet(f"""
            QComboBox {{
                padding: 6px 12px; border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: 8px; font-size: 13px; background: white;
            }}
        """)
        layout.addWidget(mode_combo)

        # API key input (shown for Azure)
        key_row = QHBoxLayout()
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("Azure OpenAI API key")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 6px 12px; border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: 8px; font-size: 13px; background: white;
            }}
        """)
        key_row.addWidget(self._key_input)

        save_key_btn = QPushButton("Save to Keychain")
        save_key_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 6px 14px; border-radius: 9999px;
                background: {CX_ACCENT}; color: white;
                font-size: 12px; font-weight: 500; border: none;
            }}
            QPushButton:hover {{ background: #C46547; }}
        """)
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
            saved_label.setStyleSheet(f"font-size: 12px; color: {CX_ACCENT}; border: none;")
            layout.addWidget(saved_label)

        hint = QLabel(
            "Azure: Enter your API key (stored in macOS Keychain).\n"
            "Local: Requires Ollama running on localhost.\n"
            "Rule-based: No API needed (offline mode)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_TERTIARY}; border: none;")
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
