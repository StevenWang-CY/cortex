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

    title: str = Field(..., description="Tab title")
    url: str = Field(..., description="Tab URL")
    tab_type: Literal[
        "documentation",
        "stackoverflow",
        "search",
        "code_host",
        "social",
        "other",
    ] = Field("other", description="Classified tab type")
    is_active: bool = Field(False, description="Whether this is the active tab")


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

    def to_llm_context(self) -> str:
        """Generate context string for LLM prompt."""
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
            parts.append(f"Tabs: {self.browser_context.tab_count}")
            parts.append(f"Active: {self.browser_context.active_tab_title}")
            if self.browser_context.active_tab_content_excerpt:
                parts.append(
                    f"Content: {self.browser_context.active_tab_content_excerpt[:500]}"
                )

        return "\n".join(parts)
