"""
Context Engine — Tab Classifier

Single source of truth for URL-based tab type classification.
Categorizes browser tabs into semantic types based on domain patterns.

This module is the canonical classifier; other modules (parser, rule_scorer)
should import ``classify_tab`` rather than implementing their own URL logic.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Domain pattern registries  (order matters — first match wins)
# ---------------------------------------------------------------------------

_EDUCATIONAL_DOMAINS = re.compile(
    r"(coursera\.org|edx\.org|udemy\.com|udacity\.com|khanacademy\.org"
    r"|brilliant\.org|codecademy\.com|freecodecamp\.org|leetcode\.com"
    r"|hackerrank\.com|mit\.edu/courses|ocw\.mit\.edu|class-central\.com"
    r"|skillshare\.com|pluralsight\.com|egghead\.io|frontendmasters\.com"
    r"|datacamp\.com|exercism\.org|codewars\.com|open\.edu"
    r"|lynda\.com|linkedin\.com/learning)",
    re.IGNORECASE,
)

_DOCUMENTATION_DOMAINS = re.compile(
    r"(docs\.|documentation|/docs/|developer\.mozilla|devdocs\.io"
    r"|readthedocs|sphinx|javadoc|rustdoc|godoc|pkg\.go\.dev"
    r"|react\.dev|vuejs\.org/guide|angular\.io/docs"
    r"|pytorch\.org/docs|numpy\.org/doc|pandas\.pydata\.org/docs"
    r"|fastapi\.tiangolo\.com|pydantic-docs"
    r"|docs\.python\.org|docs\.rs|learn\.microsoft\.com"
    r"|developer\.apple\.com/documentation"
    r"|cloud\.google\.com/docs|firebase\.google\.com/docs)",
    re.IGNORECASE,
)

_REFERENCE_DOMAINS = re.compile(
    r"(stackoverflow\.com|stackexchange\.com|wikipedia\.org"
    r"|scholar\.google\.com|semanticscholar\.org|doi\.org|dblp\.org"
    r"|arxiv\.org|openreview\.net|w3schools\.com|geeksforgeeks\.org"
    r"|tutorialspoint\.com|mdn\.io)",
    re.IGNORECASE,
)

_CODE_HOST_DOMAINS = re.compile(
    r"(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org"
    r"|sourceforge\.net|sr\.ht|gitea\.com)",
    re.IGNORECASE,
)

_AI_ASSISTANT_DOMAINS = re.compile(
    r"(chatgpt\.com|chat\.openai\.com|claude\.ai|gemini\.google\.com"
    r"|copilot\.microsoft\.com|perplexity\.ai|phind\.com|you\.com/chat"
    r"|poe\.com|bard\.google\.com|deepseek\.com)",
    re.IGNORECASE,
)

_SOCIAL_DOMAINS = re.compile(
    r"(twitter\.com|(?<![a-z])x\.com|reddit\.com|facebook\.com|instagram\.com"
    r"|threads\.net|mastodon\.social|bsky\.app|linkedin\.com(?!/learning))",
    re.IGNORECASE,
)

_ENTERTAINMENT_DOMAINS = re.compile(
    r"(netflix\.com|hulu\.com|disneyplus\.com|twitch\.tv|9gag\.com"
    r"|buzzfeed\.com|tumblr\.com|tiktok\.com|imgur\.com|giphy\.com"
    r"|spotify\.com|soundcloud\.com|pandora\.com)",
    re.IGNORECASE,
)

_VIDEO_DOMAINS = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com"
    r"|wistia\.com|loom\.com)",
    re.IGNORECASE,
)

# Ordered list: first match wins
_CLASSIFIERS: list[tuple[re.Pattern[str], str]] = [
    (_AI_ASSISTANT_DOMAINS, "ai_assistant"),
    (_EDUCATIONAL_DOMAINS, "educational"),
    (_CODE_HOST_DOMAINS, "code_host"),
    (_DOCUMENTATION_DOMAINS, "documentation"),
    (_REFERENCE_DOMAINS, "reference"),
    (_VIDEO_DOMAINS, "video"),
    (_SOCIAL_DOMAINS, "social"),
    (_ENTERTAINMENT_DOMAINS, "entertainment"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_tab(url: str, title: str = "") -> str:
    """Classify a browser tab into a semantic type.

    Args:
        url: The tab's URL (required).
        title: The tab's title (optional, reserved for future heuristics).

    Returns:
        One of: ``educational``, ``documentation``, ``reference``,
        ``code_host``, ``ai_assistant``, ``social``, ``entertainment``,
        ``video``, ``other``.
    """
    if not url:
        return "other"

    url_lower = url.lower()
    for pattern, tab_type in _CLASSIFIERS:
        if pattern.search(url_lower):
            return tab_type

    return "other"
