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

from PySide6.QtCore import Qt
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
    CX_ACCENT,
    CX_BG,
    CX_BORDER_DEFAULT,
    CX_DANGER,
    CX_SURFACE,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    RADIUS_MD,
    SP4,
    SP5,
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Connect Extensions")
        self.setFixedWidth(440)
        self.setStyleSheet(f"background: {CX_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP5, SP5, SP5, SP5)
        layout.setSpacing(SP4)

        title = QLabel("Connect Extensions")
        title.setStyleSheet(
            f"font-family: Georgia, serif; font-size: 20px; "
            f"font-weight: 600; color: {CX_TEXT};"
        )
        layout.addWidget(title)

        # Translocation warning (hidden by default)
        self._transloc_warning = QLabel(
            "Cortex is running in a temporary sandbox.\n"
            "Move it to Applications, then run in Terminal:\n"
            "  xattr -cr /Applications/Cortex.app\n"
            "and relaunch."
        )
        self._transloc_warning.setWordWrap(True)
        self._transloc_warning.setStyleSheet(
            f"background: {CX_DANGER}22; color: {CX_DANGER}; "
            f"padding: 12px; border-radius: {RADIUS_MD}px; font-size: 13px;"
        )
        self._transloc_warning.setVisible(False)
        layout.addWidget(self._transloc_warning)

        translocated = is_translocated()
        if translocated:
            self._transloc_warning.setVisible(True)

        # Browser cards
        for name, app_path, scheme in _BROWSERS:
            installed = os.path.exists(app_path)
            card = self._make_browser_card(name, app_path, scheme, installed, translocated)
            layout.addWidget(card)

        # Editor card
        editor = find_editor_cli()
        layout.addWidget(self._make_editor_card(editor, translocated))

        layout.addStretch()

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
        card.setStyleSheet(f"""
            QFrame {{
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_MD}px;
                padding: {SP4}px;
            }}
        """)
        layout = QVBoxLayout(card)

        header = QHBoxLayout()
        title = QLabel(f"{name} Extension")
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT}; border: none;")
        header.addWidget(title)
        header.addStretch()

        status = QLabel("Not installed" if not installed else "Ready")
        status.setStyleSheet(
            f"font-size: 12px; color: {CX_TEXT_TERTIARY if not installed else CX_ACCENT}; border: none;"
        )
        header.addWidget(status)
        layout.addLayout(header)

        btn = QPushButton(f"Connect {name}")
        btn.setEnabled(installed and not translocated)
        btn.setStyleSheet(f"""
            QPushButton {{
                padding: 8px 16px; border-radius: 9999px;
                background: {CX_TEXT}; color: white;
                font-size: 13px; font-weight: 500; border: none;
            }}
            QPushButton:hover {{ background: #333; }}
            QPushButton:disabled {{ background: {CX_TEXT_TERTIARY}; }}
        """)
        btn.clicked.connect(lambda checked=False, n=name, s=scheme: self._connect_browser(n, s))
        layout.addWidget(btn)

        self._instructions_label = QLabel("")
        self._instructions_label.setWordWrap(True)
        self._instructions_label.setStyleSheet(
            f"font-size: 12px; color: {CX_TEXT_SECONDARY}; border: none;"
        )
        self._instructions_label.setVisible(False)
        layout.addWidget(self._instructions_label)

        if not installed:
            hint = QLabel(f"{name} not found")
            hint.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_TERTIARY}; border: none;")
            layout.addWidget(hint)

        return card

    def _make_editor_card(
        self,
        editor: tuple[str, str] | None,
        translocated: bool,
    ) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {CX_SURFACE};
                border: 1px solid {CX_BORDER_DEFAULT};
                border-radius: {RADIUS_MD}px;
                padding: {SP4}px;
            }}
        """)
        layout = QVBoxLayout(card)

        editor_name = editor[1] if editor else "Editor"
        header = QHBoxLayout()
        title = QLabel(f"{editor_name} Extension")
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CX_TEXT}; border: none;")
        header.addWidget(title)
        header.addStretch()

        status = QLabel("Ready" if editor else "Not found")
        status.setStyleSheet(
            f"font-size: 12px; color: {CX_ACCENT if editor else CX_TEXT_TERTIARY}; border: none;"
        )
        header.addWidget(status)
        layout.addLayout(header)

        btn = QPushButton(f"Connect {editor_name}")
        btn.setEnabled(editor is not None and not translocated)
        btn.setStyleSheet(f"""
            QPushButton {{
                padding: 8px 16px; border-radius: 9999px;
                background: {CX_TEXT}; color: white;
                font-size: 13px; font-weight: 500; border: none;
            }}
            QPushButton:hover {{ background: #333; }}
            QPushButton:disabled {{ background: {CX_TEXT_TERTIARY}; }}
        """)
        if editor:
            cli_path = editor[0]
            btn.clicked.connect(lambda: self._connect_editor(cli_path, editor_name))
        layout.addWidget(btn)

        if not editor:
            hint = QLabel("No compatible editor found (VS Code, Cursor, VSCodium)")
            hint.setStyleSheet(f"font-size: 12px; color: {CX_TEXT_TERTIARY}; border: none;")
            layout.addWidget(hint)

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
