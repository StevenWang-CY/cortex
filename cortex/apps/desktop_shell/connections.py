"""Desktop Shell — Connections Panel (macOS-native refactor).

One-click setup for Chrome, Edge, and VS Code / Cursor / VSCodium. Visual
layer adopts the system semantic palette, SF system fonts, sentence-case
headings, and the popover-vibrancy material. Subprocess installation
mechanics, App Translocation detection, and the editor-CLI search are all
unchanged.
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

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
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
    SP6,
)

logger = logging.getLogger(__name__)

_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = "#5C5854"
_LABEL_TERTIARY = "#827971"
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_DANGER = SEMANTIC_LIGHT["danger"]
_DANGER_DIM = "rgba(215, 0, 21, 0.10)"
_SUCCESS = SEMANTIC_LIGHT["success"]
_SUCCESS_DIM = "rgba(48, 178, 87, 0.10)"
_WARNING_BG = "rgba(217, 161, 0, 0.12)"
_WARNING = SEMANTIC_LIGHT["warning"]


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
    for cmd, name in [("code", "VS Code"), ("cursor", "Cursor"), ("codium", "VSCodium")]:
        found = shutil.which(cmd)
        if found:
            return (found, name)
    return None


# ---------------------------------------------------------------------------
# Browsers
# ---------------------------------------------------------------------------

_BROWSERS: list[tuple[str, str, str]] = [
    ("Chrome", "/Applications/Google Chrome.app", "chrome://extensions"),
    ("Edge", "/Applications/Microsoft Edge.app", "edge://extensions"),
]


# ---------------------------------------------------------------------------
# ConnectionsPanel
# ---------------------------------------------------------------------------

class ConnectionsPanel(QWidget):
    """Popover-style panel for one-click browser + editor connect."""

    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Connect")
        self.setFixedWidth(480)
        self.setStyleSheet(f"background: {_WINDOW_BG}; color: {_LABEL};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP5)

        # ── Header (back link) ───────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
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

        # ── Title (Cormorant italic — brand preserved) ───────────────
        title = QLabel("Connect Extensions")
        title.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-size: {FS_TITLE}px;"
            "font-style: italic;"
            f"font-weight: {FW_REGULAR};"
            f"color: {_LABEL}; background: transparent;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "Link your browser and editor to enable real-time workspace "
            "restructuring."
        )
        subtitle.setWordWrap(True)
        subtitle.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        subtitle.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP2)

        # ── Translocation warning (yellow info banner) ────────────────
        self._transloc_warning = QFrame()
        self._transloc_warning.setStyleSheet(
            "QFrame {"
            f"  background: {_WARNING_BG};"
            f"  border: 0.5px solid rgba(217, 161, 0, 0.30);"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        warn_layout = QVBoxLayout(self._transloc_warning)
        warn_layout.setContentsMargins(SP4, SP3, SP4, SP3)
        warn_title = QLabel("⚠︎  App Sandbox Detected")
        warn_title.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        warn_title.setStyleSheet(
            f"color: {_WARNING}; border: none; background: transparent;"
        )
        warn_layout.addWidget(warn_title)
        warn_body = QLabel(
            "Cortex is running in a temporary sandbox. Move it to Applications, "
            "run `xattr -cr /Applications/Cortex.app` in Terminal, then relaunch."
        )
        warn_body.setWordWrap(True)
        warn_body.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        warn_body.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        warn_layout.addWidget(warn_body)
        self._transloc_warning.setVisible(False)
        layout.addWidget(self._transloc_warning)

        translocated = is_translocated()
        if translocated:
            self._transloc_warning.setVisible(True)

        # ── Browsers ──────────────────────────────────────────────────
        browser_label = QLabel("Browsers")
        browser_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        browser_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(browser_label)

        for name, app_path, scheme in _BROWSERS:
            installed = os.path.exists(app_path)
            card = self._make_browser_card(name, app_path, scheme, installed, translocated)
            layout.addWidget(card)

        # ── Editor ────────────────────────────────────────────────────
        editor_label = QLabel("Editor")
        editor_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        editor_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(editor_label)

        editor = find_editor_cli()
        layout.addWidget(self._make_editor_card(editor, translocated))

        layout.addStretch()

    # -- Lifecycle ------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="popover")
        except Exception:
            pass

    # -- Navigation -----------------------------------------------------

    def _on_back(self) -> None:
        self.hide()
        self.back_requested.emit()

    # -- Card builders --------------------------------------------------

    def _row_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            "QFrame {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        return card

    def _status_pill(self, text: str, *, ok: bool) -> QLabel:
        if ok:
            color = _SUCCESS
            bg = _SUCCESS_DIM
        else:
            color = _LABEL_TERTIARY
            bg = "rgba(0,0,0,0.05)"
        lbl = QLabel(text)
        lbl.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        lbl.setStyleSheet(
            f"color: {color}; background: {bg};"
            f" border: none; border-radius: {RADIUS_BUTTON}px;"
            "  padding: 3px 8px;"
        )
        return lbl

    def _primary_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(34)
        btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
            "QPushButton:disabled { background: rgba(0,0,0,0.12); color: rgba(0,0,0,0.35); }"
        )
        return btn

    def _make_browser_card(
        self,
        name: str,
        app_path: str,
        scheme: str,
        installed: bool,
        translocated: bool,
    ) -> QFrame:
        card = self._row_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        header = QHBoxLayout()
        header.setSpacing(SP3)
        title = QLabel(name)
        title.setFont(mac_native.system_font(FS_BODY, "semibold"))
        title.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._status_pill("Available" if installed else "Not found", ok=installed))
        layout.addLayout(header)

        desc = QLabel(
            "Install native messaging host and load the Cortex extension."
            if installed else f"{name} is not installed on this Mac."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none; background: transparent;"
        )
        layout.addWidget(desc)

        btn = self._primary_button(f"Connect {name}")
        btn.setEnabled(installed and not translocated)
        btn.clicked.connect(
            lambda checked=False, n=name, s=scheme: self._connect_browser(n, s)
        )
        layout.addWidget(btn)

        return card

    def _make_editor_card(
        self,
        editor: tuple[str, str] | None,
        translocated: bool,
    ) -> QFrame:
        card = self._row_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        editor_name = editor[1] if editor else "Editor"

        header = QHBoxLayout()
        header.setSpacing(SP3)
        title = QLabel(editor_name)
        title.setFont(mac_native.system_font(FS_BODY, "semibold"))
        title.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._status_pill(
            "Available" if editor else "Not found", ok=bool(editor),
        ))
        layout.addLayout(header)

        desc = QLabel(
            "Install the Cortex VS Code extension for editor integration."
            if editor else
            "No compatible editor found (VS Code, Cursor, VSCodium)."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none; background: transparent;"
        )
        layout.addWidget(desc)

        btn = self._primary_button(f"Connect {editor_name}")
        btn.setEnabled(editor is not None and not translocated)
        if editor:
            cli_path = editor[0]
            btn.clicked.connect(lambda: self._connect_editor(cli_path, editor_name))
        layout.addWidget(btn)
        return card

    # -- Actions --------------------------------------------------------

    def _connect_browser(self, name: str, scheme: str) -> None:
        try:
            from cortex.scripts.install_native_host import install

            app_root = str(canonical_app_path())
            if not install(project_root=app_root):
                QMessageBox.warning(
                    self,
                    f"Connect {name}",
                    "Native messaging host installation did not find a Chromium "
                    "browser profile. Open the browser once, then click Connect again.",
                )
                return
        except Exception:
            logger.exception("Failed to install native messaging host")
            QMessageBox.warning(
                self,
                f"Connect {name}",
                "Failed to install the native messaging host. Check the Cortex log "
                "and try again.",
            )
            return

        ext_subdir = (
            "browser_extension_chrome"
            if "chrome" in name.lower()
            else "browser_extension_edge"
        )
        ext_path = canonical_app_path() / "Contents" / "Resources" / ext_subdir
        if not ext_path.exists():
            QMessageBox.warning(self, "Error", f"Extension bundle not found at:\n{ext_path}")
            return

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(ext_path))

        app_name = "Google Chrome" if "chrome" in name.lower() else "Microsoft Edge"
        try:
            subprocess.Popen(["open", "-a", app_name, scheme])
        except Exception:
            logger.exception("Failed to open %s", app_name)

        QMessageBox.information(
            self,
            f"Connect {name}",
            "Extension path copied to clipboard!\n\n"
            "1. Enable Developer Mode (top-right toggle)\n"
            "2. Click 'Load unpacked'\n"
            "3. Paste the path (Cmd+V)\n"
            "4. Click the reload icon on the Cortex card",
        )

    def _connect_editor(self, cli_path: str, editor_name: str) -> None:
        # Resolve VSIX via glob so the desktop shell tracks whatever version
        # the build pipeline produced (vsix filename derives from package.json).
        if getattr(sys, "frozen", False):
            vsix_dir = canonical_app_path() / "Contents" / "Resources"
        else:
            vsix_dir = (
                Path(__file__).resolve().parents[2] / "apps" / "vscode_extension"
            )
        matches = sorted(vsix_dir.glob("cortex-somatic-*.vsix"))
        vsix = matches[-1] if matches else vsix_dir / "cortex-somatic.vsix"

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
