"""
Context Engine — App Classifier

Determines the active workspace mode based on the currently focused
application and available context signals.

Modes:
- coding_debugging: VS Code focused with diagnostics
- reading_docs: Chrome focused on documentation sites
- browsing: Chrome focused on mixed tabs
- terminal_errors: Terminal focused with detected errors
- mixed: Multiple signals, no dominant mode

Uses OS-level window focus tracking (via telemetry engine's WindowTracker)
to identify the active application.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from cortex.libs.schemas.context import BrowserContext, EditorContext, TerminalContext

logger = logging.getLogger(__name__)

WorkspaceMode = Literal[
    "coding_debugging", "reading_docs", "browsing", "terminal_errors", "mixed"
]
ActiveApp = Literal["vscode", "chrome", "terminal", "other"]

# App name patterns for classification
_VSCODE_PATTERNS = re.compile(
    r"(visual studio code|code|code - oss|codium|cursor)", re.IGNORECASE
)
_CHROME_PATTERNS = re.compile(
    r"(google chrome|chrome|chromium|brave|firefox|safari|arc|edge)", re.IGNORECASE
)
_TERMINAL_PATTERNS = re.compile(
    r"(terminal|iterm|warp|alacritty|kitty|hyper|wezterm|konsole|gnome-terminal|xterm)",
    re.IGNORECASE,
)

# Documentation URL patterns
_DOC_URL_PATTERNS = re.compile(
    r"(docs\.|documentation|/docs/|developer\.mozilla|devdocs\.io"
    r"|readthedocs|sphinx|javadoc|rustdoc|godoc|pkg\.go\.dev"
    r"|react\.dev|vuejs\.org/guide|angular\.io/docs"
    r"|pytorch\.org/docs|numpy\.org/doc|pandas\.pydata\.org/docs"
    r"|fastapi\.tiangolo\.com|pydantic-docs)",
    re.IGNORECASE,
)


def classify_app(app_name: str | None) -> ActiveApp:
    """
    Classify the active application from its name/title.

    Args:
        app_name: Application name or window title.

    Returns:
        Classified application type.
    """
    if app_name is None:
        return "other"

    if _VSCODE_PATTERNS.search(app_name):
        return "vscode"
    if _CHROME_PATTERNS.search(app_name):
        return "chrome"
    if _TERMINAL_PATTERNS.search(app_name):
        return "terminal"

    return "other"


def classify_mode(
    active_app: ActiveApp,
    editor_context: EditorContext | None = None,
    browser_context: BrowserContext | None = None,
    terminal_context: TerminalContext | None = None,
) -> WorkspaceMode:
    """
    Determine workspace mode from active app and available contexts.

    Args:
        active_app: Currently focused application.
        editor_context: VS Code context if available.
        browser_context: Browser context if available.
        terminal_context: Terminal context if available.

    Returns:
        Classified workspace mode.
    """
    if active_app == "vscode" and editor_context is not None:
        if editor_context.error_count > 0:
            return "coding_debugging"
        return "coding_debugging"  # VS Code = coding regardless

    if active_app == "terminal" and terminal_context is not None:
        if terminal_context.has_errors:
            return "terminal_errors"
        return "mixed"

    if active_app == "chrome" and browser_context is not None:
        doc_count = browser_context.tab_type_classification.get("documentation", 0)
        total = browser_context.tab_count
        if total > 0 and doc_count / total > 0.5:
            return "reading_docs"
        return "browsing"

    # Fallback: check what contexts are available
    if editor_context is not None and editor_context.error_count > 0:
        return "coding_debugging"
    if terminal_context is not None and terminal_context.has_errors:
        return "terminal_errors"

    return "mixed"


def classify_tab_type(url: str) -> str:
    """
    Classify a browser tab by its URL.

    Args:
        url: Tab URL.

    Returns:
        Tab type: "documentation", "stackoverflow", "search",
        "code_host", "social", or "other".
    """
    url_lower = url.lower()

    if "stackoverflow.com" in url_lower or "stackexchange.com" in url_lower:
        return "stackoverflow"

    if _DOC_URL_PATTERNS.search(url_lower):
        return "documentation"

    if any(s in url_lower for s in ("google.com/search", "bing.com/search", "duckduckgo.com")):
        return "search"

    if any(s in url_lower for s in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")):
        return "code_host"

    if any(s in url_lower for s in (
        "twitter.com", "x.com", "reddit.com", "facebook.com",
        "youtube.com", "discord.com", "slack.com",
    )):
        return "social"

    return "other"
