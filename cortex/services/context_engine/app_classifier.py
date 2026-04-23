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
_PDF_URL_PATTERNS = re.compile(r"(\.pdf(?:$|\?)|arxiv\.org/pdf/|openreview\.net/pdf)", re.IGNORECASE)
_PAPER_URL_PATTERNS = re.compile(
    r"(arxiv\.org/abs/|openreview\.net/forum|acm\.org/doi|ieeexplore\.ieee\.org|paperswithcode\.com/paper)",
    re.IGNORECASE,
)
_REFERENCE_URL_PATTERNS = re.compile(
    r"(wikipedia\.org|scholar\.google\.com|semanticscholar\.org|doi\.org|dblp\.org)",
    re.IGNORECASE,
)
_AI_ASSISTANT_PATTERNS = re.compile(
    r"(gemini\.google\.com|chatgpt\.com|chat\.openai\.com|claude\.ai|"
    r"copilot\.microsoft\.com|perplexity\.ai|phind\.com|you\.com/chat|"
    r"poe\.com|bard\.google\.com)",
    re.IGNORECASE,
)
_VIDEO_PLATFORM_PATTERNS = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com)",
    re.IGNORECASE,
)
_COMMUNICATION_PATTERNS = re.compile(
    r"(slack\.com|discord\.com|teams\.microsoft\.com)",
    re.IGNORECASE,
)
_SOCIAL_URL_PATTERNS = re.compile(
    r"(twitter\.com|x\.com|reddit\.com|facebook\.com)",
    re.IGNORECASE,
)
_DISTRACTION_URL_PATTERNS = re.compile(
    r"(instagram\.com|tiktok\.com|netflix\.com|twitch\.tv|9gag\.com|buzzfeed\.com|tumblr\.com)",
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
        reading_count = sum(
            browser_context.tab_type_classification.get(kind, 0)
            for kind in ("documentation", "paper", "pdf", "reference")
        )
        total = browser_context.tab_count
        if total > 0 and reading_count / total > 0.4:
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

    if _PDF_URL_PATTERNS.search(url_lower):
        return "pdf"

    if _PAPER_URL_PATTERNS.search(url_lower):
        return "paper"

    if _REFERENCE_URL_PATTERNS.search(url_lower):
        return "reference"

    if _DOC_URL_PATTERNS.search(url_lower):
        return "documentation"

    if any(s in url_lower for s in ("google.com/search", "bing.com/search", "duckduckgo.com")):
        return "search"

    if any(s in url_lower for s in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")):
        return "code_host"

    if _AI_ASSISTANT_PATTERNS.search(url_lower):
        return "ai_assistant"

    if _VIDEO_PLATFORM_PATTERNS.search(url_lower):
        return "video_platform"

    if _COMMUNICATION_PATTERNS.search(url_lower):
        return "communication"

    if _SOCIAL_URL_PATTERNS.search(url_lower):
        return "social"

    if _DISTRACTION_URL_PATTERNS.search(url_lower):
        return "distraction"

    return "other"


_AMBIGUOUS_TYPES = {
    "ai_assistant",
    "video_platform",
    "social",
    "communication",
    "distraction",
    "other",
}

# Short technical terms that should be treated as valid goal keywords
# despite being <= 2 characters. Mirrors TECH_SHORT_WORDS in background.ts.
_TECH_SHORT_WORDS = frozenset({
    "go", "ml", "ai", "css", "sql", "vue", "rx", "aws", "gcp", "api",
    "cli", "gui", "dom", "npm", "pip", "git", "ux", "ui", "db",
    "os", "ci", "cd", "qa", "c++", "c#", "r", "dx", "io", "jwt",
})


def _extract_goal_keywords(goal: str) -> list[str]:
    """Extract meaningful keywords from a goal string.

    Keeps words > 2 chars plus recognized tech short words (ML, AI, Go, etc.).
    Uses word-boundary matching to prevent false positives from common substrings.
    """
    words = goal.lower().replace("-", " ").replace("_", " ").split()
    return [
        w for w in words
        if len(w) > 2 or w in _TECH_SHORT_WORDS
    ]


def classify_tab_type_with_goal(url: str, title: str, goal: str) -> str:
    """Goal-aware tab classification.

    If the tab's title contains keywords from the user's focus goal AND the
    base type is ambiguous (video, social, communication, distraction, other),
    reclassify as ``goal_relevant`` so downstream systems never recommend
    closing it.

    Uses word-boundary matching to prevent false positives (e.g., "project"
    in goal matching "HTML project starter" unrelated to the actual goal).
    """
    base_type = classify_tab_type(url)
    if not goal or base_type not in _AMBIGUOUS_TYPES:
        return base_type

    keywords = _extract_goal_keywords(goal)
    title_lower = title.lower()
    for kw in keywords:
        # Use word-boundary matching to avoid substring false positives
        if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
            return "goal_relevant"
    return base_type
