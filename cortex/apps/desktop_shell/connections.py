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
    FONT_MONO,
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
# Warm-greyscale label tints from the token registry. Tertiary is the
# WCAG-AA-passing value the dashboard adopted in F55; audit Wave 2
# promoted it so every surface picks it up.
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY
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

# F17 (Phase-4 audit): factor app-bundle discovery into a single helper
# so the user's home-folder install path (``~/Applications``) and the
# system path (``/Applications``) both work. Previously every editor /
# browser hardcoded ``/Applications/Foo.app`` and silently failed when
# the user installed under ``~/Applications`` — a common pattern on
# macOS for multi-user setups and corporate-managed devices.
def _find_app_bundle(
    bundle_name: str,
    cli_subpath: str | None = None,
) -> Path | None:
    """Locate a macOS ``.app`` bundle by name.

    Checks (in order):
      1. ``/Applications/<bundle_name>`` (system-wide install)
      2. ``~/Applications/<bundle_name>`` (per-user install)
      3. ``~/Applications/Chrome Apps/<bundle_name>`` (rare PWA install)

    Returns the bundle ``Path`` if found, or ``None`` if no candidate
    exists. When ``cli_subpath`` is provided, requires that the
    embedded CLI file is present and executable; this filters out
    partial / corrupted installs.
    """
    candidates = [
        Path("/Applications") / bundle_name,
        Path.home() / "Applications" / bundle_name,
        Path.home() / "Applications" / "Chrome Apps" / bundle_name,
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if cli_subpath is not None:
            cli = candidate / cli_subpath
            if not cli.is_file() or not os.access(str(cli), os.X_OK):
                continue
        return candidate
    return None


def _resolve_editor_cli(bundle_name: str, cli_subpath: str) -> str | None:
    """Return the absolute CLI path for an editor bundle, if installed."""
    bundle = _find_app_bundle(bundle_name, cli_subpath=cli_subpath)
    if bundle is None:
        return None
    return str(bundle / cli_subpath)


# Each candidate is (display_name, [bundle_name, cli_subpath, extra
# fallback abs path]). The legacy ``/usr/local/bin/<cli>`` symlink path
# is still checked first because users who ``code --install …`` get
# that shim installed, and it is the canonical "this editor's CLI is
# on PATH" answer.
_EDITOR_CANDIDATES: list[tuple[str, str, str, list[str]]] = [
    (
        "VS Code",
        "Visual Studio Code.app",
        "Contents/Resources/app/bin/code",
        ["/usr/local/bin/code"],
    ),
    (
        "VS Code Insiders",
        "Visual Studio Code - Insiders.app",
        "Contents/Resources/app/bin/code-insiders",
        ["/usr/local/bin/code-insiders"],
    ),
    (
        "Cursor",
        "Cursor.app",
        "Contents/Resources/app/bin/cursor",
        ["/usr/local/bin/cursor"],
    ),
    (
        "VSCodium",
        "VSCodium.app",
        "Contents/Resources/app/bin/codium",
        ["/usr/local/bin/codium"],
    ),
]


def find_editor_cli() -> tuple[str, str] | None:
    """Return (cli_path, display_name) for the first available editor."""
    for name, bundle, subpath, fallbacks in _EDITOR_CANDIDATES:
        # First try the legacy ``/usr/local/bin/`` symlink — fastest
        # path when the user has run ``Shell Command: Install 'code'``.
        for p in fallbacks:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return (p, name)
        # Then resolve the bundle (system or per-user Applications).
        resolved = _resolve_editor_cli(bundle, subpath)
        if resolved is not None:
            return (resolved, name)
    # ``shutil.which`` is the last resort — catches custom PATH installs.
    for cmd, name in [("code", "VS Code"), ("cursor", "Cursor"), ("codium", "VSCodium")]:
        found = shutil.which(cmd)
        if found:
            return (found, name)
    return None


# ---------------------------------------------------------------------------
# Browsers
# ---------------------------------------------------------------------------

# Each entry is (display_name, bundle_name, settings_url). The bundle
# is resolved via ``_find_app_bundle`` so per-user ``~/Applications``
# installs work.
_BROWSER_BUNDLES: list[tuple[str, str, str]] = [
    ("Chrome", "Google Chrome.app", "chrome://extensions"),
    ("Edge", "Microsoft Edge.app", "edge://extensions"),
]


def _resolve_browser_bundles() -> list[tuple[str, str, str]]:
    """Return (name, abs_bundle_path, settings_url) for installed browsers.

    The legacy ``_BROWSERS`` constant hardcoded ``/Applications/...``;
    this helper lifts ``~/Applications`` installs into parity.
    """
    out: list[tuple[str, str, str]] = []
    for name, bundle, url in _BROWSER_BUNDLES:
        path = _find_app_bundle(bundle)
        if path is None:
            # Preserve the legacy /Applications/ fallback so callers
            # that show a CTA "install Chrome" still have a sensible
            # default to display.
            out.append((name, f"/Applications/{bundle}", url))
        else:
            out.append((name, str(path), url))
    return out


_BROWSERS: list[tuple[str, str, str]] = _resolve_browser_bundles()


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

        # audit-w2 (F55 carry-over): keep button refs so we can chain
        # tab order at the end of __init__ once every card is built.
        self._tab_order_chain: list[QPushButton] = []

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
        set_accessible_name(back_btn, "Back to dashboard")
        # Phase J-5: keyboard-reachable Back button.
        try:
            back_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._tab_order_chain.append(back_btn)
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
        self._transloc_warning.setObjectName("CortexTranslocWarn")
        self._transloc_warning.setStyleSheet(
            "QFrame#CortexTranslocWarn {"
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
            "then run this command in Terminal and relaunch:"
        )
        warn_body.setWordWrap(True)
        warn_body.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        warn_body.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        warn_layout.addWidget(warn_body)
        warn_cmd = QLabel("xattr -cr /Applications/Cortex.app")
        warn_cmd.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        warn_cmd.setStyleSheet(
            f"font-family: {FONT_MONO};"
            f"font-size: {FS_CAPTION}px;"
            f"color: {_LABEL};"
            f"background: rgba(0,0,0,0.06);"
            f"border: 0.5px solid rgba(0,0,0,0.10);"
            f"border-radius: {RADIUS_BUTTON}px;"
            "padding: 4px 8px;"
        )
        warn_layout.addWidget(warn_cmd)
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

        # audit-w2 (F55 carry-over): chain tab order across every action
        # button. VoiceOver users can now walk Back → browser Connect
        # buttons → editor Connect with the keyboard alone.
        chain_tab_order(*self._tab_order_chain)

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
        card.setObjectName("CortexConnRow")
        # Give cards a comfortable minimum height so Qt can't squish
        # their inner widgets into invisibility on small windows.
        card.setMinimumHeight(110)
        card.setStyleSheet(
            "QFrame#CortexConnRow {"
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
        # Phase J-5: ensure every primary action button on the
        # connections panel is keyboard-reachable. QPushButton's
        # default on macOS can fall back to WheelFocus which excludes
        # the button from the tab cycle.
        try:
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
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

    def _secondary_button(self, text: str) -> QPushButton:
        """Outlined secondary action (e.g. 'Verify connection')."""
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(30)
        btn.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        try:
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        btn.setStyleSheet(
            "QPushButton {"
            "  padding: 5px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {BRAND_ACCENT};"
            f"  border: 1px solid {BRAND_ACCENT};"
            "}"
            "QPushButton:hover { background: rgba(0,0,0,0.04); }"
            "QPushButton:disabled { color: rgba(0,0,0,0.30); border-color: rgba(0,0,0,0.18); }"
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
        set_accessible_name(btn, f"Connect {name}")
        set_accessible_description(
            btn,
            f"Install Cortex native messaging host for {name} and open "
            f"{name}'s extensions page so you can load the Cortex extension.",
        )
        self._tab_order_chain.append(btn)
        layout.addWidget(btn)

        # Honest verification affordance: loading an unpacked extension is
        # a manual step the desktop shell cannot perform, so after the
        # guide the user clicks "Verify connection" to confirm the pieces
        # the shell CAN check — the native-messaging manifest is installed
        # and the daemon is reachable for the extension to connect to.
        verify_btn = self._secondary_button("Verify connection")
        verify_btn.setEnabled(installed and not translocated)
        verify_btn.clicked.connect(
            lambda checked=False, n=name: self._verify_browser_connection(n)
        )
        set_accessible_name(verify_btn, f"Verify {name} connection")
        set_accessible_description(
            verify_btn,
            f"Check that the Cortex native messaging host is installed for "
            f"{name} and that the Cortex daemon is running.",
        )
        self._tab_order_chain.append(verify_btn)
        layout.addWidget(verify_btn)

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
        set_accessible_name(btn, f"Connect {editor_name}")
        set_accessible_description(
            btn,
            f"Install the Cortex extension into {editor_name}.",
        )
        self._tab_order_chain.append(btn)
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
            f"Finish connecting {name}",
            "The native messaging host is installed and the extension "
            "path is copied to your clipboard.\n\n"
            "Loading an unpacked extension is a manual step Cortex cannot "
            "do for you — follow these steps in the window that just "
            "opened:\n\n"
            "1. Enable Developer Mode (top-right toggle)\n"
            "2. Click 'Load unpacked'\n"
            "3. Paste the path (Cmd+V) and choose the folder\n"
            "4. Pin the Cortex extension\n\n"
            "When you're done, click 'Verify connection' to confirm.",
        )

    def _verify_browser_connection(self, name: str) -> None:
        """Honestly report what the desktop shell CAN confirm about the
        browser connection: the native-messaging manifest is installed
        for this browser, and the Cortex daemon is reachable for the
        extension to connect to. Loading the unpacked extension is a
        manual step the shell cannot observe directly, so we never claim
        the extension is 'connected' — we report each verifiable piece."""
        manifest_ok = self._native_host_manifest_installed(name)
        daemon_ok = self._daemon_reachable()

        if manifest_ok and daemon_ok:
            QMessageBox.information(
                self,
                f"Verify {name}",
                "Native messaging host: installed ✓\n"
                "Cortex daemon: running ✓\n\n"
                "Both prerequisites are in place. If the Cortex extension "
                "is loaded and pinned, it will connect automatically. The "
                "extension popup shows the live connection status.",
            )
            return

        lines = [
            f"Native messaging host: {'installed ✓' if manifest_ok else 'NOT installed ✗'}",
            f"Cortex daemon: {'running ✓' if daemon_ok else 'not reachable ✗'}",
            "",
        ]
        if not manifest_ok:
            lines.append(
                "Click 'Connect' to (re)install the native messaging host, "
                "then fully quit and relaunch the browser."
            )
        if not daemon_ok:
            lines.append(
                "Start Cortex (the daemon listens on port 9473) so the "
                "extension has something to connect to."
            )
        QMessageBox.warning(self, f"Verify {name}", "\n".join(lines))

    def _native_host_manifest_installed(self, name: str) -> bool:
        """Return True iff the Cortex native-messaging manifest exists in
        the browser's NativeMessagingHosts directory for this user.

        Host name + profile roots mirror
        ``cortex.scripts.install_native_host`` exactly so a successful
        install is reflected here."""
        host_name = "com.cortex.launcher"
        support = Path.home() / "Library" / "Application Support"
        if "edge" in name.lower():
            roots = [support / "Microsoft Edge"]
        else:
            roots = [
                support / "Google" / "Chrome",
                support / "Chromium",
            ]
        for root in roots:
            manifest = root / "NativeMessagingHosts" / f"{host_name}.json"
            if manifest.exists():
                return True
        return False

    def _daemon_reachable(self, *, host: str = "127.0.0.1", port: int = 9473) -> bool:
        """Best-effort TCP reachability probe for the daemon WebSocket
        port. A successful connect means the extension has somewhere to
        connect to. 250 ms timeout so the UI never stalls."""
        import socket

        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

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
