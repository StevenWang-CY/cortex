"""
Cortex Context Schemas

Pydantic models for workspace context gathered from VS Code, browser,
and terminal adapters.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Diagnostic(BaseModel):
    """Code diagnostic (error, warning, info) from editor."""

    severity: Literal["error", "warning", "info", "hint"] = Field(
        ..., description="Diagnostic severity level"
    )
    message: str = Field(..., description="Diagnostic message text")
    line: int = Field(..., ge=1, description="Line number (1-indexed)")
    column: int = Field(0, ge=0, description="Column number (0-indexed)")
    source: str | None = Field(None, description="Source of diagnostic (e.g., 'typescript')")
    code: str | None = Field(None, description="Diagnostic code (e.g., 'TS2322')")


class EditorContext(BaseModel):
    """Context from VS Code or other editor."""

    file_path: str = Field(..., description="Path to current file")
    visible_range: tuple[int, int] = Field(
        ..., description="Start and end lines of visible range"
    )
    symbol_at_cursor: str | None = Field(
        None, description="Current function/class/symbol name"
    )
    diagnostics: list[Diagnostic] = Field(
        default_factory=list, description="Errors/warnings in current file"
    )
    recent_edits: list[str] = Field(
        default_factory=list, description="Recent edit descriptions"
    )
    visible_code: str = Field("", description="Code visible in viewport")

    @property
    def error_count(self) -> int:
        """Count of error-level diagnostics."""
        return sum(1 for d in self.diagnostics if d.severity == "error")

    @property
    def warning_count(self) -> int:
        """Count of warning-level diagnostics."""
        return sum(1 for d in self.diagnostics if d.severity == "warning")

    @property
    def visible_lines(self) -> int:
        """Number of visible lines."""
        return self.visible_range[1] - self.visible_range[0] + 1


class TerminalContext(BaseModel):
    """Context from terminal output."""

    last_n_lines: list[str] = Field(
        default_factory=list, description="Last N lines of terminal output"
    )
    detected_errors: list[str] = Field(
        default_factory=list, description="Detected error messages"
    )
    repeated_commands: list[str] = Field(
        default_factory=list, description="Commands run multiple times"
    )
    running_command: str | None = Field(
        None, description="Currently running command"
    )

    @property
    def has_errors(self) -> bool:
        """Check if terminal has detected errors."""
        return len(self.detected_errors) > 0

    @property
    def error_summary(self) -> str:
        """Get a summary of detected errors."""
        if not self.detected_errors:
            return ""
        if len(self.detected_errors) == 1:
            return self.detected_errors[0]
        return f"{self.detected_errors[0]} (+{len(self.detected_errors) - 1} more)"


class TabInfo(BaseModel):
    """Information about a browser tab."""

    tab_id: int = Field(-1, description="Chrome runtime tab ID for action targeting")
    title: str = Field(..., description="Tab title")
    url: str = Field(..., description="Tab URL")
    tab_type: Literal[
        "documentation",
        "ai_assistant",
        "reference",
        "paper",
        "pdf",
        "stackoverflow",
        "search",
        "code_host",
        "learning_platform",
        "video_platform",
        "communication",
        "distraction",
        "social",
        "other",
    ] = Field("other", description="Classified tab type")
    is_active: bool = Field(False, description="Whether this is the active tab")
    topic_hint: str = Field("", description="Extracted topic/query from tab title")


class BrowserContext(BaseModel):
    """Context from browser extension."""

    active_tab_title: str = Field(..., description="Active tab title")
    active_tab_url: str = Field(..., description="Active tab URL")
    active_tab_content_excerpt: str = Field(
        "", description="Excerpt of active tab content (max 2000 tokens)"
    )
    all_tabs: list[TabInfo] = Field(
        default_factory=list, description="Information about all open tabs"
    )
    tab_type_classification: dict[str, int] = Field(
        default_factory=dict,
        description="Count of tabs by type",
    )
    focus_goal: str | None = Field(
        None, description="Current focus session goal, if active"
    )

    @property
    def tab_count(self) -> int:
        """Total number of open tabs."""
        return len(self.all_tabs)

    @property
    def documentation_tabs(self) -> int:
        """Number of documentation tabs."""
        return self.tab_type_classification.get("documentation", 0)

    @property
    def stackoverflow_tabs(self) -> int:
        """Number of StackOverflow tabs."""
        return self.tab_type_classification.get("stackoverflow", 0)


class TaskContext(BaseModel):
    """
    Complete workspace context for LLM scaffolding.

    Assembled from all workspace adapters before sending to LLM.
    """

    mode: Literal[
        "coding_debugging",
        "reading_docs",
        "browsing",
        "terminal_errors",
        "mixed",
    ] = Field(..., description="Detected workspace mode")
    active_app: Literal["vscode", "chrome", "terminal", "other"] = Field(
        ..., description="Currently active application"
    )
    current_goal_hint: str | None = Field(
        None, description="Inferred user goal from context"
    )
    complexity_score: float = Field(
        ..., ge=0.0, le=1.0, description="Workspace complexity score"
    )
    editor_context: EditorContext | None = Field(
        None, description="VS Code context if available"
    )
    terminal_context: TerminalContext | None = Field(
        None, description="Terminal context if available"
    )
    browser_context: BrowserContext | None = Field(
        None, description="Browser context if available"
    )
    learned_relevance: dict[str, float] = Field(
        default_factory=dict,
        description="Per-domain learned relevance scores from user feedback",
    )

    @property
    def has_editor(self) -> bool:
        """Check if editor context is available."""
        return self.editor_context is not None

    @property
    def has_terminal(self) -> bool:
        """Check if terminal context is available."""
        return self.terminal_context is not None

    @property
    def has_browser(self) -> bool:
        """Check if browser context is available."""
        return self.browser_context is not None

    @property
    def total_errors(self) -> int:
        """Count total errors across all contexts."""
        count = 0
        if self.editor_context:
            count += self.editor_context.error_count
        if self.terminal_context:
            count += len(self.terminal_context.detected_errors)
        return count

    @property
    def is_high_complexity(self) -> bool:
        """Check if workspace is highly complex (intervention threshold)."""
        return self.complexity_score >= 0.6

    def to_llm_context(self, *, learned_relevance: dict[str, float] | None = None) -> str:
        """Generate context string for LLM prompt."""
        # Use model field as fallback if kwarg not passed
        if learned_relevance is None and self.learned_relevance:
            learned_relevance = self.learned_relevance
        parts = []

        parts.append(f"Mode: {self.mode}")
        parts.append(f"Active App: {self.active_app}")
        parts.append(f"Complexity: {self.complexity_score:.2f}")

        if self.current_goal_hint:
            parts.append(f"Goal: {self.current_goal_hint}")

        if self.editor_context:
            parts.append("\n--- Editor Context ---")
            parts.append(f"File: {self.editor_context.file_path}")
            if self.editor_context.symbol_at_cursor:
                parts.append(f"Symbol: {self.editor_context.symbol_at_cursor}")
            if self.editor_context.error_count > 0:
                parts.append(f"Errors: {self.editor_context.error_count}")
                for d in self.editor_context.diagnostics[:3]:
                    if d.severity == "error":
                        parts.append(f"  Line {d.line}: {d.message[:100]}")
            if self.editor_context.visible_code:
                parts.append(f"Code:\n{self.editor_context.visible_code[:1500]}")

        if self.terminal_context and self.terminal_context.has_errors:
            parts.append("\n--- Terminal Errors ---")
            for err in self.terminal_context.detected_errors[:3]:
                parts.append(err[:200])

        if self.browser_context:
            parts.append("\n--- Browser Context ---")
            parts.append(f"Total tabs: {self.browser_context.tab_count}")
            parts.append(f"Active: {self.browser_context.active_tab_title}")
            if self.browser_context.focus_goal:
                parts.append(f"Focus goal: {self.browser_context.focus_goal}")

            # List tabs with integer indices for LLM tab targeting.
            # Pre-filter to max 30 tabs to stay within token budget.
            tabs = self.browser_context.all_tabs
            selected = _select_tabs_for_llm(tabs, max_tabs=30)
            for idx, tab in selected:
                topic = f' — "{tab.topic_hint}"' if tab.topic_hint else ""
                parts.append(
                    f"  Tab {idx}: [{tab.tab_type}] {tab.title[:80]}{topic} — {tab.url[:120]}"
                )
            remainder = len(tabs) - len(selected)
            if remainder > 0:
                type_counts = self.browser_context.tab_type_classification
                summary_parts = [f"{v} {k}" for k, v in type_counts.items() if v > 0]
                parts.append(
                    f"  ... and {remainder} more tabs ({', '.join(summary_parts)})"
                )

            if self.browser_context.active_tab_content_excerpt:
                parts.append(
                    f"Content: {self.browser_context.active_tab_content_excerpt[:500]}"
                )

        # Inject learned tab relevance preferences
        if learned_relevance:
            prefs = [
                f"User previously kept {domain} open during similar sessions (relevance: {score:.2f})"
                for domain, score in learned_relevance.items()
                if score > 0.6
            ]
            if prefs:
                parts.append("\n--- Learned Preferences ---")
                parts.extend(prefs[:5])

        return "\n".join(parts)


# Priority order for tab type selection (higher priority types kept first)
_TAB_TYPE_PRIORITY: dict[str, int] = {
    "documentation": 0,
    "ai_assistant": 1,
    "reference": 2,
    "paper": 3,
    "pdf": 4,
    "stackoverflow": 5,
    "code_host": 6,
    "learning_platform": 7,
    "search": 8,
    "video_platform": 9,
    "communication": 10,
    "other": 11,
    "social": 12,
    "distraction": 13,
}


def _select_tabs_for_llm(
    tabs: list[TabInfo], *, max_tabs: int = 30
) -> list[tuple[int, TabInfo]]:
    """Select and index tabs for LLM context, capped at *max_tabs*.

    Selection: active tab first, then deduplicate by hostname,
    then prioritize by tab type (reference/code > distraction).
    Returns list of ``(original_index, tab)`` tuples.
    """
    if len(tabs) <= max_tabs:
        return list(enumerate(tabs))

    indexed = list(enumerate(tabs))
    # Active tab always first
    active = [(i, t) for i, t in indexed if t.is_active]
    rest = [(i, t) for i, t in indexed if not t.is_active]

    # Deduplicate by hostname (keep first per host)
    seen_hosts: set[str] = set()
    for _, t in active:
        try:
            host = t.url.split("//", 1)[1].split("/", 1)[0]
            seen_hosts.add(host)
        except (IndexError, ValueError):
            pass

    deduped: list[tuple[int, TabInfo]] = []
    duplicates: list[tuple[int, TabInfo]] = []
    for i, t in rest:
        try:
            host = t.url.split("//", 1)[1].split("/", 1)[0]
        except (IndexError, ValueError):
            host = t.url
        if host not in seen_hosts:
            seen_hosts.add(host)
            deduped.append((i, t))
        else:
            duplicates.append((i, t))

    # Sort by type priority (lower = higher priority)
    deduped.sort(key=lambda x: _TAB_TYPE_PRIORITY.get(x[1].tab_type, 7))

    selected = active + deduped
    # Fill remaining slots with duplicates if needed
    remaining_slots = max_tabs - len(selected)
    if remaining_slots > 0:
        duplicates.sort(key=lambda x: _TAB_TYPE_PRIORITY.get(x[1].tab_type, 7))
        selected.extend(duplicates[:remaining_slots])

    return selected[:max_tabs]
