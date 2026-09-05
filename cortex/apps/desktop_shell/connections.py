"""Desktop Shell — Connections window (macOS-native refactor).

One-click setup for Chrome, Edge, and VS Code / Cursor / VSCodium.

Every outcome is rendered inline on the card it belongs to — a three-row
checklist (bridge / extension / Cortex running) plus numbered next steps
that stay on screen — instead of modal message boxes that vanish the
moment the user clicks OK. The panel re-probes what it can verify each
time it is shown, Escape closes it, and it is a window (no "Back").

Subprocess installation mechanics, App Translocation detection, and the
editor-CLI search are unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
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
from cortex.apps.desktop_shell.components import status_pill_qss, wrap_capped
from cortex.apps.desktop_shell.tokens import (
    BTN_ACCENT_QSS,
    BTN_GHOST_QSS,
    CARD_QSS,
    CX_BG,
    CX_DANGER_TEXT,
    CX_SUCCESS_TEXT,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    CX_WARNING_TEXT,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    PAGE_TITLE_QSS,
    RADIUS_CARD,
    SECTION_HEADING_QSS,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
)

logger = logging.getLogger(__name__)

_WARNING_BG = "rgba(217, 161, 0, 0.12)"
_DAEMON_PORT = 9473


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


def _browser_app_name(name: str) -> str:
    return "Google Chrome" if "chrome" in name.lower() else "Microsoft Edge"


def _probe_bridge(app_name: str) -> tuple[bool, bool, str] | None:
    """Run the real native-host protocol probe for one browser.

    Returns ``(bridge_ok, extension_found, error)`` or ``None`` when the
    probe itself could not run (missing installer module, crash).
    """
    try:
        from cortex.scripts.install_native_host import verify_browser_installation

        verification = verify_browser_installation(app_name)
        return (
            bool(verification.ok),
            bool(getattr(verification, "extension_ids", ())),
            str(getattr(verification, "error", "") or ""),
        )
    except Exception:
        logger.debug("bridge probe failed for %s", app_name, exc_info=True)
        return None


def _finish_steps(name: str) -> list[str]:
    """The browser-required steps after the bridge is registered."""
    return [
        f"In {name}, turn on Developer mode (top-right toggle).",
        "Click “Load unpacked”, press Cmd+Shift+G in the folder picker, "
        "paste the copied path, press Return, then click Open.",
        f"Quit {name} fully with Cmd+Q and reopen it — the bridge is only "
        "picked up on a fresh launch.",
        "Pin Cortex, open its popup, then click Verify here.",
    ]


_ROW_COPY: dict[str, tuple[str, str, str]] = {
    # key: (done, not done, not checked yet)
    "bridge": (
        "Browser bridge registered",
        "Browser bridge not registered",
        "Browser bridge — not checked yet",
    ),
    "extension": (
        "Cortex extension found in the browser",
        "Cortex extension not found in the browser",
        "Cortex extension — load it, then Verify",
    ),
    "editor": (
        "Editor found",
        "No compatible editor found",
        "Editor — not checked yet",
    ),
    "installed": (
        "Cortex extension installed",
        "Cortex extension not installed",
        "Cortex extension — not installed yet",
    ),
    "daemon": (
        "Cortex is running",
        "Cortex is not running",
        "Cortex — not checked yet",
    ),
}

_TONE_COLOR: dict[str, str] = {
    "neutral": CX_TEXT_SECONDARY,
    "success": CX_SUCCESS_TEXT,
    "warning": CX_WARNING_TEXT,
    "danger": CX_DANGER_TEXT,
}


# ---------------------------------------------------------------------------
# Inline checklist
# ---------------------------------------------------------------------------

class _Checklist(QWidget):
    """Three verifiable rows plus numbered next steps, rendered inline on a
    connection card so the outcome of Connect / Verify never disappears."""

    def __init__(self, row_keys: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SP2)
        for key in row_keys:
            row = QLabel("")
            row.setFont(mac_native.system_font(FS_CAPTION, "medium"))
            row.setWordWrap(True)
            set_accessible_name(row, _ROW_COPY[key][2].split(" —")[0] + " status")
            self._rows[key] = row
            layout.addWidget(row)
            self.set_row(key, None)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._note.setVisible(False)
        set_accessible_name(self._note, "Connection note")
        layout.addWidget(self._note)
        self._steps = QLabel("")
        self._steps.setWordWrap(True)
        self._steps.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._steps.setStyleSheet(
            f"color: {CX_TEXT}; border: none; background: transparent;"
        )
        self._steps.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._steps.setVisible(False)
        set_accessible_name(self._steps, "Next steps")
        layout.addWidget(self._steps)

    def set_row(self, key: str, state: bool | None) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        done, failed, pending = _ROW_COPY[key]
        if state is True:
            row.setText(f"✓  {done}")
            color = CX_SUCCESS_TEXT
        elif state is False:
            row.setText(f"✗  {failed}")
            color = CX_DANGER_TEXT
        else:
            row.setText(f"○  {pending}")
            color = CX_TEXT_TERTIARY
        row.setStyleSheet(f"color: {color}; border: none; background: transparent;")

    def set_note(self, text: str, tone: str) -> None:
        self._note.setText(text)
        self._note.setStyleSheet(
            f"color: {_TONE_COLOR.get(tone, CX_TEXT_SECONDARY)};"
            " border: none; background: transparent;"
        )
        self._note.setVisible(bool(text))

    def set_steps(self, steps: list[str]) -> None:
        if not steps:
            self._steps.setText("")
            self._steps.setVisible(False)
            return
        self._steps.setText(
            "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
        )
        self._steps.setVisible(True)


# ---------------------------------------------------------------------------
# ConnectionsPanel
# ---------------------------------------------------------------------------

class ConnectionsPanel(QWidget):
    """Window for one-click browser + editor connect with inline results."""

    back_requested = Signal()
    # Emitted with the display name when every locally verifiable layer
    # for that browser passes (bridge, extension present, Cortex running).
    connection_verified = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex — Connect")
        self.setFixedWidth(480)
        self.setMinimumHeight(560)
        self.setStyleSheet(f"background: {CX_BG}; color: {CX_TEXT};")
        set_accessible_name(self, "Connect extensions")
        set_accessible_description(
            self, "Connect the Cortex browser and editor extensions. Escape closes.",
        )

        # Plain status model per card so tests and the host can read what
        # the checklist shows without walking widgets.
        self._status_model: dict[str, dict[str, Any]] = {}
        self._checklists: dict[str, _Checklist] = {}
        self._browser_cards: list[tuple[str, str, bool]] = []
        self._editor_name: str = "Editor"
        self._editor_found = False
        self._reprobe_on_show = True

        # audit-w2 (F55 carry-over): keep button refs so we can chain
        # tab order at the end of __init__ once every card is built.
        self._tab_order_chain: list[QPushButton] = []

        try:
            from PySide6.QtWidgets import QScrollArea
        except ImportError:  # pragma: no cover - test stub path
            QScrollArea = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        content.setObjectName("CortexConnectionsContent")
        content.setStyleSheet(
            f"#CortexConnectionsContent {{ background: {CX_BG}; }}"
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP5)

        if QScrollArea is not None:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            try:
                scroll.setFrameShape(QFrame.Shape.NoFrame)
            except AttributeError:  # pragma: no cover - stub harness
                pass
            scroll.setStyleSheet(
                "QScrollArea { border: none; background: transparent; }"
                "QScrollBar:vertical { background: transparent; width: 8px; }"
                "QScrollBar::handle:vertical {"
                "  background: rgba(0,0,0,0.18); border-radius: 4px; min-height: 24px;"
                "}"
                "QScrollBar::handle:vertical:hover { background: rgba(0,0,0,0.32); }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            )
            scroll.setWidget(content)
            outer.addWidget(scroll)
        else:  # pragma: no cover - test stub path
            outer.addWidget(content)

        # ── Title ────────────────────────────────────────────────────
        title = QLabel("Connect extensions")
        title.setStyleSheet(PAGE_TITLE_QSS)
        layout.addWidget(title)

        subtitle = QLabel(
            "Link your browser and editor so Cortex can see what you're "
            "working on and act on it. Results stay on each card."
        )
        subtitle.setWordWrap(True)
        subtitle.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        subtitle.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP2)

        # ── Translocation warning ────────────────────────────────────
        self._transloc_warning = QFrame()
        self._transloc_warning.setObjectName("CortexTranslocWarn")
        self._transloc_warning.setStyleSheet(
            "QFrame#CortexTranslocWarn {"
            f"  background: {_WARNING_BG};"
            "  border: 1px solid rgba(217, 161, 0, 0.30);"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        warn_layout = QVBoxLayout(self._transloc_warning)
        warn_layout.setContentsMargins(SP4, SP3, SP4, SP3)
        warn_title = QLabel("Cortex is running from a temporary location")
        warn_title.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        warn_title.setStyleSheet(
            f"color: {CX_WARNING_TEXT}; border: none; background: transparent;"
        )
        warn_layout.addWidget(warn_title)
        warn_body = QLabel(
            "macOS opened this copy in a sandbox, so the browser bridge can't "
            "be registered. Move Cortex into your Applications folder and open "
            "it from there. If this keeps happening, re-download Cortex from "
            "the release page and verify the download before opening it."
        )
        warn_body.setWordWrap(True)
        warn_body.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        warn_body.setStyleSheet(
            f"color: {CX_TEXT}; border: none; background: transparent;"
        )
        warn_layout.addWidget(warn_body)
        self._transloc_warning.setVisible(False)
        layout.addWidget(self._transloc_warning)

        translocated = is_translocated()
        self._translocated = translocated
        if translocated:
            self._transloc_warning.setVisible(True)

        # ── Browsers ──────────────────────────────────────────────────
        browser_label = QLabel("Browsers")
        browser_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(browser_label)

        for name, app_path, scheme in _BROWSERS:
            installed = os.path.exists(app_path)
            self._browser_cards.append((name, scheme, installed))
            card = self._make_browser_card(name, app_path, scheme, installed, translocated)
            layout.addWidget(card)

        # ── Editor ────────────────────────────────────────────────────
        editor_label = QLabel("Editor")
        editor_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(editor_label)

        editor = find_editor_cli()
        layout.addWidget(self._make_editor_card(editor, translocated))

        layout.addStretch()

        # audit-w2 (F55 carry-over): chain tab order across every action
        # button so a keyboard user walks Connect → Verify → editor
        # Connect without bouncing through labels.
        chain_tab_order(*self._tab_order_chain)

    # -- Lifecycle ------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self)
        except Exception:
            pass
        if getattr(self, "_reprobe_on_show", False):
            try:
                self.reprobe()
            except Exception:
                logger.debug("connections re-probe failed", exc_info=True)

    def keyPressEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        key = getattr(event, "key", lambda: None)()
        if key == Qt.Key.Key_Escape:
            self._on_back()
            try:
                event.accept()  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        super().keyPressEvent(event)

    # -- Navigation -----------------------------------------------------

    def _on_back(self) -> None:
        self.hide()
        self.back_requested.emit()

    # -- Status model ---------------------------------------------------

    def _set_status(
        self,
        name: str,
        *,
        rows: dict[str, bool | None] | None = None,
        steps: list[str] | None = None,
        note: str | None = None,
        tone: str | None = None,
    ) -> None:
        """Merge an update into the card's status model and re-render."""
        model = self.__dict__.setdefault("_status_model", {})
        entry = model.setdefault(
            name, {"rows": {}, "steps": [], "note": "", "tone": "neutral"},
        )
        if rows:
            entry["rows"].update(rows)
        if steps is not None:
            entry["steps"] = list(steps)
        if note is not None:
            entry["note"] = note
        if tone is not None:
            entry["tone"] = tone
        self._render_status(name)

    def _render_status(self, name: str) -> None:
        checklist = getattr(self, "_checklists", {}).get(name)
        entry = getattr(self, "_status_model", {}).get(name)
        if checklist is None or entry is None:
            return
        try:
            for key, state in entry["rows"].items():
                checklist.set_row(key, state)
            checklist.set_note(str(entry["note"]), str(entry["tone"]))
            checklist.set_steps(list(entry["steps"]))
        except Exception:
            logger.debug("checklist render failed for %s", name, exc_info=True)

    def status_for(self, name: str) -> dict[str, Any]:
        """Read-only view of a card's checklist model (for hosts/tests)."""
        entry = getattr(self, "_status_model", {}).get(name, {})
        return {
            "rows": dict(entry.get("rows", {})),
            "steps": list(entry.get("steps", [])),
            "note": str(entry.get("note", "")),
            "tone": str(entry.get("tone", "neutral")),
        }

    def reprobe(self) -> None:
        """Re-check everything the shell can verify without user action:
        bridge registration + extension presence per installed browser,
        editor presence, and whether Cortex is running."""
        daemon_ok = self._daemon_reachable()
        for name, _scheme, installed in getattr(self, "_browser_cards", []):
            if not installed or getattr(self, "_translocated", False):
                continue
            probe = _probe_bridge(_browser_app_name(name))
            rows: dict[str, bool | None] = {"daemon": daemon_ok}
            if probe is not None:
                bridge_ok, extension_found, _error = probe
                rows["bridge"] = bridge_ok
                rows["extension"] = extension_found if bridge_ok else None
            entry = getattr(self, "_status_model", {}).get(name, {})
            steps = list(entry.get("steps", []))
            if probe is not None and not probe[0] and not steps:
                steps = [f"Click Connect {name} to register the browser bridge."]
            self._set_status(name, rows=rows, steps=steps)
        editor_name = getattr(self, "_editor_name", "Editor")
        self._set_status(
            editor_name,
            rows={"editor": getattr(self, "_editor_found", False), "daemon": daemon_ok},
        )

    # -- Card builders --------------------------------------------------

    def _row_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("CortexConnRow")
        card.setStyleSheet(f"QFrame#CortexConnRow {{ {CARD_QSS} }}")
        return card

    def _status_pill(self, text: str, *, ok: bool) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        lbl.setStyleSheet(status_pill_qss("success" if ok else "neutral"))
        return lbl

    def _primary_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(34)
        btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        # Phase J-5: every action button is keyboard-reachable.
        try:
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        btn.setStyleSheet(BTN_ACCENT_QSS)
        return btn

    def _secondary_button(self, text: str) -> QPushButton:
        """Ghost secondary action (e.g. 'Verify connection')."""
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(30)
        btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        try:
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        btn.setStyleSheet(BTN_GHOST_QSS)
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
            f"color: {CX_TEXT}; border: none; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(
            self._status_pill(
                "Browser found" if installed else "Not installed",
                ok=installed,
            )
        )
        layout.addLayout(header)

        desc = QLabel(
            "Registers the Cortex bridge, copies the extension folder, and "
            "opens the extensions page so you can load it."
            if installed else f"{name} is not installed on this Mac."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; border: none; background: transparent;"
        )
        layout.addWidget(desc)

        checklist = _Checklist(("bridge", "extension", "daemon"))
        self._checklists[name] = checklist
        layout.addWidget(checklist)
        self._set_status(name, rows={"bridge": None, "extension": None, "daemon": None})

        actions = QHBoxLayout()
        actions.setSpacing(SP3)
        btn = self._primary_button(f"Connect {name}")
        btn.setEnabled(installed and not translocated)
        btn.clicked.connect(
            lambda checked=False, n=name, s=scheme: self._connect_browser(n, s)
        )
        set_accessible_name(btn, f"Connect {name}")
        set_accessible_description(
            btn,
            f"Register the Cortex bridge for {name} and open {name}'s "
            "extensions page so you can load the Cortex extension.",
        )
        self._tab_order_chain.append(btn)
        actions.addWidget(btn)

        # Honest verification affordance: loading an unpacked extension is
        # a manual step the desktop shell cannot perform, so after the
        # guide the user clicks "Verify" to confirm the pieces the shell
        # CAN check.
        verify_btn = self._secondary_button("Verify")
        verify_btn.setEnabled(installed and not translocated)
        verify_btn.clicked.connect(
            lambda checked=False, n=name: self._verify_browser_connection(n)
        )
        set_accessible_name(verify_btn, f"Verify {name} connection")
        set_accessible_description(
            verify_btn,
            f"Check that the Cortex bridge is registered for {name}, the "
            "extension is loaded, and Cortex is running.",
        )
        self._tab_order_chain.append(verify_btn)
        actions.addWidget(verify_btn)
        actions.addStretch()
        layout.addLayout(actions)

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
        self._editor_name = editor_name
        self._editor_found = editor is not None

        header = QHBoxLayout()
        header.setSpacing(SP3)
        title = QLabel(editor_name)
        title.setFont(mac_native.system_font(FS_BODY, "semibold"))
        title.setStyleSheet(
            f"color: {CX_TEXT}; border: none; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._status_pill(
            "Editor found" if editor else "Not found", ok=bool(editor),
        ))
        layout.addLayout(header)

        desc = QLabel(
            "Installs the Cortex extension into your editor."
            if editor else
            "No compatible editor found (VS Code, Cursor, VSCodium)."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; border: none; background: transparent;"
        )
        wrap_capped(desc, 400)
        layout.addWidget(desc)

        checklist = _Checklist(("editor", "installed", "daemon"))
        self._checklists[editor_name] = checklist
        layout.addWidget(checklist)
        self._set_status(
            editor_name,
            rows={"editor": editor is not None, "installed": None, "daemon": None},
        )

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
        actions = QHBoxLayout()
        actions.addWidget(btn)
        actions.addStretch()
        layout.addLayout(actions)
        return card

    # -- Actions --------------------------------------------------------

    def _connect_browser(self, name: str, scheme: str) -> None:
        app_name = _browser_app_name(name)
        self._set_status(
            name,
            rows={"bridge": None},
            steps=[],
            note=f"Registering the {name} bridge…",
            tone="neutral",
        )
        try:
            from cortex.scripts.install_native_host import (
                install,
                verify_browser_installation,
            )

            app_root = str(canonical_app_path())
            if not install(
                project_root=app_root,
                target_browsers=(app_name,),
            ):
                self._set_status(
                    name,
                    rows={"bridge": False},
                    note=(
                        f"Cortex could not register its browser bridge for {name}. "
                        "No connection was reported as successful."
                    ),
                    tone="danger",
                    steps=[
                        "Make sure Cortex is in your Applications folder.",
                        f"Click Connect {name} again.",
                    ],
                )
                return
            verification = verify_browser_installation(app_name)
            if not verification.ok:
                raise RuntimeError(
                    verification.error or "native host protocol verification failed"
                )
        except Exception as exc:
            logger.exception("Failed to install native messaging host")
            self._set_status(
                name,
                rows={"bridge": False},
                note=(
                    "Cortex could not install and verify the browser bridge. "
                    "No connection was reported as successful. "
                    f"Details: {str(exc)[:300]}"
                ),
                tone="danger",
                steps=[
                    "Make sure Cortex is in your Applications folder.",
                    f"Click Connect {name} again.",
                ],
            )
            return

        ext_subdir = (
            "browser_extension_chrome"
            if "chrome" in name.lower()
            else "browser_extension_edge"
        )
        ext_path = canonical_app_path() / "Contents" / "Resources" / ext_subdir
        if not ext_path.exists():
            self._set_status(
                name,
                rows={"bridge": True, "extension": False},
                note=f"Bridge registered, but the extension folder is missing at {ext_path}.",
                tone="danger",
                steps=[
                    "Re-download Cortex from the release page and verify the "
                    "download, then try again.",
                ],
            )
            return

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(ext_path))

        try:
            opened = subprocess.run(
                ["/usr/bin/open", "-a", app_name, scheme],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if opened.returncode != 0:
                detail = (opened.stderr or opened.stdout or "unknown error").strip()
                raise RuntimeError(detail)
        except Exception as exc:
            logger.exception("Failed to open %s", app_name)
            self._set_status(
                name,
                rows={"bridge": True, "extension": None, "daemon": self._daemon_reachable()},
                note=(
                    f"The bridge passed a real protocol check, but Cortex could not "
                    f"open {name}'s extensions page. Open {scheme} yourself, then "
                    f"finish these steps. Details: {str(exc)[:200]}"
                ),
                tone="warning",
                steps=_finish_steps(name),
            )
            return

        self._set_status(
            name,
            rows={"bridge": True, "extension": None, "daemon": self._daemon_reachable()},
            note=(
                "The bridge passed a real protocol check. The extension folder "
                f"is on your clipboard — finish these steps in {name}:"
            ),
            tone="success",
            steps=_finish_steps(name),
        )

    def _verify_browser_connection(self, name: str) -> None:
        """Report what the desktop shell CAN confirm about the browser
        connection: the bridge is registered and answers, the extension
        is present in the browser profile, and Cortex is running for it
        to connect to. Live WebSocket status is the popup's to show."""
        from cortex.scripts.install_native_host import verify_browser_installation

        app_name = _browser_app_name(name)
        verification = verify_browser_installation(app_name)
        host_ok = bool(verification.ok)
        extension_found = bool(getattr(verification, "extension_ids", ()))
        daemon_ok = self._daemon_reachable()

        rows: dict[str, bool | None] = {
            "bridge": host_ok,
            "extension": extension_found,
            "daemon": daemon_ok,
        }
        if host_ok and extension_found and daemon_ok:
            self._set_status(
                name,
                rows=rows,
                note=(
                    "Everything Cortex can check here is ready. The extension "
                    "popup shows the live connection."
                ),
                tone="success",
                steps=[],
            )
            try:
                self.connection_verified.emit(name)
            except Exception:
                logger.debug("connection_verified emit failed", exc_info=True)
            return

        steps: list[str] = []
        if not host_ok:
            detail = getattr(verification, "error", None) or "verification failed"
            steps.append(
                f"Click Connect {name} to repair the browser bridge. Details: {detail}"
            )
        if not extension_found:
            steps.append(
                "Load the copied extension folder, then quit the browser fully "
                "with Cmd+Q and reopen it."
            )
        if not daemon_ok:
            steps.append(
                "Start a session in Cortex so the extension has something to "
                "connect to."
            )
        self._set_status(
            name,
            rows=rows,
            note="Not connected yet — the checks below say what is missing.",
            tone="danger",
            steps=steps,
        )

    def _native_host_manifest_installed(self, name: str) -> bool:
        """Compatibility wrapper for the strong native-host verification.

        The historical method name is retained for callers/tests, but a file's
        mere existence no longer counts as installed. The exact manifest target
        must complete a framed protocol round-trip.
        """

        from cortex.scripts.install_native_host import verify_browser_installation

        browser = "Microsoft Edge" if "edge" in name.lower() else "Google Chrome"
        try:
            return verify_browser_installation(browser).ok
        except (OSError, RuntimeError, ValueError):
            return False

    def _daemon_reachable(self, *, host: str = "127.0.0.1", port: int = _DAEMON_PORT) -> bool:
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
            self._set_status(
                editor_name,
                rows={"editor": True, "installed": False},
                note=f"The extension package is missing at {vsix}.",
                tone="danger",
                steps=[
                    "Re-download Cortex from the release page and verify the "
                    "download, then try again.",
                ],
            )
            return

        self._set_status(
            editor_name,
            rows={"editor": True, "installed": None},
            note=f"Installing into {editor_name}…",
            tone="neutral",
            steps=[],
        )
        try:
            result = subprocess.run(
                [cli_path, "--install-extension", str(vsix)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                self._set_status(
                    editor_name,
                    rows={"installed": True, "daemon": self._daemon_reachable()},
                    note=f"Installed into {editor_name}.",
                    tone="success",
                    steps=[
                        f"Reload {editor_name} (Cmd+Shift+P → “Developer: Reload Window”).",
                        "Start a session in Cortex — the editor's status bar item "
                        "connects on its own.",
                    ],
                )
            else:
                self._set_status(
                    editor_name,
                    rows={"installed": False},
                    note=f"Installation failed: {result.stderr[:300]}",
                    tone="danger",
                    steps=[
                        "Try again. If it keeps failing, install the .vsix from the "
                        "Extensions view (… menu → Install from VSIX).",
                    ],
                )
        except subprocess.TimeoutExpired:
            self._set_status(
                editor_name,
                rows={"installed": False},
                note="Installation timed out.",
                tone="danger",
                steps=[f"Quit {editor_name} fully, then try again."],
            )
        except FileNotFoundError:
            self._set_status(
                editor_name,
                rows={"editor": False, "installed": False},
                note=f"{editor_name} command-line tool not found at {cli_path}.",
                tone="danger",
                steps=[
                    f"In {editor_name}, run “Shell Command: Install 'code' command "
                    "in PATH”, then try again.",
                ],
            )
