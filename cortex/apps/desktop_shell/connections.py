"""
Desktop Shell — Connections Panel

One-click setup for Chrome, Edge, and VS Code / Cursor / VSCodium.
Handles native messaging host installation, clipboard path copy, and
editor extension installation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
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
    CX_DANGER,
    CX_DANGER_DIM,
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
# App Translocation guard
# ---------------------------------------------------------------------------

def is_translocated() -> bool:
    """Detect if running from a macOS App Translocation sandbox."""
    exe = Path(sys.executable).resolve()
    return "/AppTranslocation/" in str(exe)


def canonical_app_path() -> Path:
    """Return the expected install path (not the translocated path)."""
    return Path("/Applications/Cortex.app")


# ---------------------------------------------------------------------------
# Editor detection
# ---------------------------------------------------------------------------

_EDITOR_CANDIDATES: list[tuple[str, list[str]]] = [
    ("VS Code", [
        "/usr/local/bin/code",
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    ]),
    ("VS Code Insiders", [
        "/usr/local/bin/code-insiders",
        "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders",
    ]),
    ("Cursor", [
        "/usr/local/bin/cursor",
        "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
    ]),
    ("VSCodium", [
        "/usr/local/bin/codium",
        "/Applications/VSCodium.app/Contents/Resources/app/bin/codium",
    ]),
]


def find_editor_cli() -> tuple[str, str] | None:
    """Return (cli_path, display_name) for the first available editor."""
    for name, paths in _EDITOR_CANDIDATES:
        for p in paths:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return (p, name)
    # Fallback: check $PATH (works when launched from terminal)
    for cmd, name in [("code", "VS Code"), ("cursor", "Cursor"), ("codium", "VSCodium")]:
        found = shutil.which(cmd)
        if found:
            return (found, name)
    return None


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------

_BROWSERS: list[tuple[str, str, str]] = [
    # (display_name, app_path, extensions_scheme)
    ("Chrome", "/Applications/Google Chrome.app", "chrome://extensions"),
    ("Edge", "/Applications/Microsoft Edge.app", "edge://extensions"),
]


# ---------------------------------------------------------------------------
# Connections Window
# ---------------------------------------------------------------------------

class ConnectionsPanel(QWidget):
    """Panel with one-click buttons for Chrome, Edge, and editor setup."""

    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Connect")
        self.setFixedWidth(460)
        self.setStyleSheet(f"background: {CX_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP5)

        # ── Header with back button ──────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

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

        # ── Title ────────────────────────────────────────────────────
        title = QLabel("Connect Extensions")
        title.setStyleSheet(PAGE_TITLE_QSS)
        layout.addWidget(title)

        subtitle = QLabel(
            "Link your browser and editor to enable real-time "
            "workspace restructuring."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; "
            f"color: {CX_TEXT_SECONDARY}; background: transparent; "
            f"line-height: 1.5;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP2)

        # ── Translocation warning (hidden by default) ────────────────
        self._transloc_warning = QFrame()
        self._transloc_warning.setStyleSheet(f"""
            QFrame {{
                background: {CX_DANGER_DIM};
                border: 1px solid rgba(217, 87, 87, 0.15);
                border-radius: {RADIUS_MD}px;
            }}
        """)
        warn_layout = QVBoxLayout(self._transloc_warning)
        warn_layout.setContentsMargins(SP4, SP3, SP4, SP3)
        warn_title = QLabel("App Sandbox Detected")
        warn_title.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 13px; "
            f"font-weight: 600; color: {CX_DANGER}; border: none;"
        )
        warn_layout.addWidget(warn_title)
        warn_body = QLabel(
            "Cortex is running in a temporary sandbox. "
            "Move it to Applications, run xattr -cr /Applications/Cortex.app "
            "in Terminal, then relaunch."
        )
        warn_body.setWordWrap(True)
        warn_body.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_DANGER}; border: none; line-height: 1.4;"
        )
        warn_layout.addWidget(warn_body)
        self._transloc_warning.setVisible(False)
        layout.addWidget(self._transloc_warning)

        translocated = is_translocated()
        if translocated:
            self._transloc_warning.setVisible(True)

        # ── Section: Browsers ────────────────────────────────────────
        browser_label = QLabel("BROWSERS")
        browser_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(browser_label)

        for name, app_path, scheme in _BROWSERS:
            installed = os.path.exists(app_path)
            card = self._make_browser_card(name, app_path, scheme, installed, translocated)
            layout.addWidget(card)

        # ── Section: Editor ──────────────────────────────────────────
        editor_label = QLabel("EDITOR")
        editor_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(editor_label)

        editor = find_editor_cli()
        layout.addWidget(self._make_editor_card(editor, translocated))

        layout.addStretch()

    # -- Navigation -----------------------------------------------------------

    def _on_back(self) -> None:
        self.hide()
        self.back_requested.emit()

    # -- Card builders --------------------------------------------------------

    def _make_browser_card(
        self,
        name: str,
        app_path: str,
        scheme: str,
        installed: bool,
        translocated: bool,
    ) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ {CARD_QSS} }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(SP3)

        title = QLabel(f"{name}")
        title.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 14px; "
            f"font-weight: 600; color: {CX_TEXT}; border: none;"
        )
        header.addWidget(title)
        header.addStretch()

        # Status badge
        if installed:
            status_text = "Available"
            status_color = CX_SUCCESS
            status_bg = CX_SUCCESS_DIM
        else:
            status_text = "Not found"
            status_color = CX_TEXT_TERTIARY
            status_bg = "rgba(0,0,0,0.04)"

        status = QLabel(status_text)
        status.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; font-weight: 500; "
            f"color: {status_color}; background: {status_bg}; "
            f"border: none; border-radius: {RADIUS_SM}px; "
            f"padding: 3px 8px;"
        )
        header.addWidget(status)
        layout.addLayout(header)

        # Description
        desc = QLabel(
            f"Install native messaging host and load the Cortex extension."
            if installed else f"{name} is not installed on this Mac."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(desc)

        # Connect button
        btn = QPushButton(f"Connect {name}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(installed and not translocated)
        btn.setFixedHeight(36)
        btn.setStyleSheet(BTN_PRIMARY_QSS)
        btn.clicked.connect(lambda checked=False, n=name, s=scheme: self._connect_browser(n, s))
        layout.addWidget(btn)

        # Instructions label (shown after clicking Connect)
        self._instructions_label = QLabel("")
        self._instructions_label.setWordWrap(True)
        self._instructions_label.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none;"
        )
        self._instructions_label.setVisible(False)
        layout.addWidget(self._instructions_label)

        return card

    def _make_editor_card(
        self,
        editor: tuple[str, str] | None,
        translocated: bool,
    ) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"QFrame {{ {CARD_QSS} }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        editor_name = editor[1] if editor else "Editor"

        # Header row
        header = QHBoxLayout()
        header.setSpacing(SP3)

        title = QLabel(editor_name)
        title.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 14px; "
            f"font-weight: 600; color: {CX_TEXT}; border: none;"
        )
        header.addWidget(title)
        header.addStretch()

        if editor:
            status_text = "Available"
            status_color = CX_SUCCESS
            status_bg = CX_SUCCESS_DIM
        else:
            status_text = "Not found"
            status_color = CX_TEXT_TERTIARY
            status_bg = "rgba(0,0,0,0.04)"

        status = QLabel(status_text)
        status.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 11px; font-weight: 500; "
            f"color: {status_color}; background: {status_bg}; "
            f"border: none; border-radius: {RADIUS_SM}px; "
            f"padding: 3px 8px;"
        )
        header.addWidget(status)
        layout.addLayout(header)

        # Description
        desc_text = (
            f"Install the Cortex VS Code extension for editor integration."
            if editor else
            "No compatible editor found (VS Code, Cursor, VSCodium)."
        )
        desc = QLabel(desc_text)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-family: {CX_FONT_SANS}; font-size: 12px; "
            f"color: {CX_TEXT_SECONDARY}; border: none; line-height: 1.4;"
        )
        layout.addWidget(desc)

        # Connect button
        btn = QPushButton(f"Connect {editor_name}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(editor is not None and not translocated)
        btn.setFixedHeight(36)
        btn.setStyleSheet(BTN_PRIMARY_QSS)
        if editor:
            cli_path = editor[0]
            btn.clicked.connect(lambda: self._connect_editor(cli_path, editor_name))
        layout.addWidget(btn)

        return card

    # -- Actions --------------------------------------------------------------

    def _connect_browser(self, name: str, scheme: str) -> None:
        """Install native host, copy extension path, open browser."""
        try:
            # 1. Install native messaging host
            from cortex.scripts.install_native_host import install

            app_root = str(canonical_app_path())
            install(project_root=app_root)
        except Exception:
            logger.exception("Failed to install native messaging host")

        # 2. Determine extension path
        ext_subdir = "browser_extension_chrome" if "chrome" in name.lower() else "browser_extension_edge"
        ext_path = canonical_app_path() / "Contents" / "Resources" / ext_subdir

        # 3. Copy to clipboard
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(ext_path))

        # 4. Open browser to extensions page
        app_name = "Google Chrome" if "chrome" in name.lower() else "Microsoft Edge"
        try:
            subprocess.Popen(["open", "-a", app_name, scheme])
        except Exception:
            logger.exception("Failed to open %s", app_name)

        # 5. Show instructions
        QMessageBox.information(
            self,
            f"Connect {name}",
            f"Extension path copied to clipboard!\n\n"
            f"1. Enable Developer Mode (top-right toggle)\n"
            f"2. Click 'Load unpacked'\n"
            f"3. Paste the path (Cmd+V)\n"
            f"4. Click the reload icon on the Cortex card",
        )

    def _connect_editor(self, cli_path: str, editor_name: str) -> None:
        """Install the VS Code extension via CLI."""
        # Find bundled .vsix
        if getattr(sys, "frozen", False):
            vsix = canonical_app_path() / "Contents" / "Resources" / "cortex-somatic-0.1.0.vsix"
        else:
            vsix = Path(__file__).resolve().parents[2] / "apps" / "vscode_extension" / "cortex-somatic-0.1.0.vsix"

        if not vsix.exists():
            QMessageBox.warning(self, "Error", f"VSIX not found at:\n{vsix}")
            return

        try:
            result = subprocess.run(
                [cli_path, "--install-extension", str(vsix)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                QMessageBox.information(
                    self,
                    "Success",
                    f"{editor_name} extension installed!\n\nReload {editor_name} to activate.",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    f"Installation failed:\n{result.stderr[:300]}",
                )
        except subprocess.TimeoutExpired:
            QMessageBox.warning(self, "Error", "Installation timed out")
        except FileNotFoundError:
            QMessageBox.warning(self, "Error", f"{editor_name} CLI not found at:\n{cli_path}")
