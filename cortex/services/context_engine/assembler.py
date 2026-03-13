"""
Context Engine — Context Assembler

Gathers context from all adapters (editor, browser, terminal) and
assembles a unified TaskContext with complexity scoring.

Complexity score (0-1) is computed from 7 factors:
1. Error density (terminal + editor diagnostics)
2. Visual clutter (visible code lines)
3. Tab overload (browser tab count)
4. Window thrashing (tab switch frequency)
5. Code block size (visible line count)
6. Diagnostic severity (weighted error/warning count)
7. Document density (content size)

Threshold: complexity_score >= 0.6 required for intervention.

Usage:
    assembler = ContextAssembler(
        editor_adapter=editor, browser_adapter=browser,
        terminal_adapter=terminal,
    )
    context = await assembler.build_context()
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from cortex.libs.schemas.context import (
    BrowserContext,
    EditorContext,
    TaskContext,
    TerminalContext,
)
from cortex.services.context_engine.app_classifier import (
    ActiveApp,
    classify_app,
    classify_mode,
)
from cortex.services.context_engine.browser_adapter import BrowserAdapter
from cortex.services.context_engine.editor_adapter import EditorAdapter
from cortex.services.context_engine.terminal_adapter import TerminalAdapter

logger = logging.getLogger(__name__)


class ContextAssembler:
    """
    Assembles TaskContext from all workspace adapters.

    Gathers editor, browser, and terminal context, classifies the
    workspace mode, and computes a complexity score.

    Usage:
        assembler = ContextAssembler(
            editor_adapter=editor,
            browser_adapter=browser,
            terminal_adapter=terminal,
        )
        context = await assembler.build_context()
    """

    def __init__(
        self,
        editor_adapter: EditorAdapter | None = None,
        browser_adapter: BrowserAdapter | None = None,
        terminal_adapter: TerminalAdapter | None = None,
        active_app_provider: Any | None = None,
        window_switch_rate: float = 0.0,
    ) -> None:
        """
        Args:
            editor_adapter: VS Code adapter instance.
            browser_adapter: Chrome adapter instance.
            terminal_adapter: Terminal adapter instance.
            active_app_provider: Callable returning current app name.
            window_switch_rate: Current window switch rate (from telemetry).
        """
        self._editor = editor_adapter
        self._browser = browser_adapter
        self._terminal = terminal_adapter
        self._active_app_provider = active_app_provider
        self._window_switch_rate = window_switch_rate

    def set_window_switch_rate(self, rate: float) -> None:
        """Update the current window switch rate from telemetry."""
        self._window_switch_rate = rate

    async def build_context(
        self,
        include_editor: bool = True,
        include_terminal: bool = True,
        include_browser: bool = True,
    ) -> TaskContext:
        """
        Build a complete TaskContext from all available adapters.

        Args:
            include_editor: Whether to gather editor context.
            include_terminal: Whether to gather terminal context.
            include_browser: Whether to gather browser context.

        Returns:
            Assembled TaskContext with complexity score.
        """
        # Determine active app
        active_app = self._get_active_app()

        # Gather contexts from adapters
        editor_ctx = await self._gather_editor() if include_editor else None
        browser_ctx = await self._gather_browser() if include_browser else None
        terminal_ctx = self._gather_terminal() if include_terminal else None

        # Classify workspace mode
        mode = classify_mode(active_app, editor_ctx, browser_ctx, terminal_ctx)

        # Compute complexity score
        complexity = compute_complexity_score(
            editor_context=editor_ctx,
            browser_context=browser_ctx,
            terminal_context=terminal_ctx,
            window_switch_rate=self._window_switch_rate,
        )

        return TaskContext(
            mode=mode,
            active_app=active_app,
            current_goal_hint=self._infer_goal(editor_ctx, terminal_ctx),
            complexity_score=complexity,
            editor_context=editor_ctx,
            terminal_context=terminal_ctx,
            browser_context=browser_ctx,
        )

    def build_context_sync(
        self,
        editor_context: EditorContext | None = None,
        browser_context: BrowserContext | None = None,
        terminal_context: TerminalContext | None = None,
        active_app: ActiveApp = "other",
    ) -> TaskContext:
        """
        Build TaskContext synchronously from pre-gathered contexts.

        Useful when contexts are already available (e.g., from push updates).
        """
        mode = classify_mode(active_app, editor_context, browser_context, terminal_context)
        complexity = compute_complexity_score(
            editor_context=editor_context,
            browser_context=browser_context,
            terminal_context=terminal_context,
            window_switch_rate=self._window_switch_rate,
        )

        return TaskContext(
            mode=mode,
            active_app=active_app,
            current_goal_hint=self._infer_goal(editor_context, terminal_context),
            complexity_score=complexity,
            editor_context=editor_context,
            terminal_context=terminal_context,
            browser_context=browser_context,
        )

    def _get_active_app(self) -> ActiveApp:
        """Determine the current active application."""
        if self._active_app_provider is not None:
            try:
                app_name = self._active_app_provider()
                return classify_app(app_name)
            except Exception:
                pass
        return "other"

    async def _gather_editor(self) -> EditorContext | None:
        """Gather editor context, falling back to cached."""
        if self._editor is None:
            return None
        ctx = await self._editor.get_context()
        return ctx or self._editor.last_context

    async def _gather_browser(self) -> BrowserContext | None:
        """Gather browser context, falling back to cached."""
        if self._browser is None:
            return None
        ctx = await self._browser.get_context()
        return ctx or self._browser.last_context

    def _gather_terminal(self) -> TerminalContext | None:
        """Gather terminal context (synchronous)."""
        if self._terminal is None:
            if self._editor is not None and hasattr(self._editor, "last_terminal_context"):
                return self._editor.last_terminal_context
            return None
        context = self._terminal.get_context()
        if context is not None:
            return context
        if self._editor is not None and hasattr(self._editor, "last_terminal_context"):
            return self._editor.last_terminal_context
        return None

    @staticmethod
    def _infer_goal(
        editor_ctx: EditorContext | None,
        terminal_ctx: TerminalContext | None,
    ) -> str | None:
        """Infer user's current goal from context signals."""
        if editor_ctx is not None:
            if editor_ctx.error_count > 0:
                first_error = next(
                    (d for d in editor_ctx.diagnostics if d.severity == "error"),
                    None,
                )
                if first_error:
                    return f"Fixing {first_error.message[:80]}"
            if editor_ctx.symbol_at_cursor:
                return f"Working on {editor_ctx.symbol_at_cursor}"

        if terminal_ctx is not None and terminal_ctx.has_errors:
            return f"Debugging: {terminal_ctx.error_summary[:80]}"

        return None


def compute_complexity_score(
    editor_context: EditorContext | None = None,
    browser_context: BrowserContext | None = None,
    terminal_context: TerminalContext | None = None,
    window_switch_rate: float = 0.0,
) -> float:
    """
    Compute workspace complexity score from available context.

    Factors (7 components, equally weighted):
    1. Error density — terminal errors + editor diagnostics
    2. Visual clutter — visible code lines
    3. Tab overload — browser tab count
    4. Window thrashing — switch rate from telemetry
    5. Code block size — visible line count
    6. Diagnostic severity — weighted error/warning count
    7. Document density — content excerpt length

    Returns:
        Complexity score in [0.0, 1.0].
    """
    scores: list[float] = []

    # 1. Error density
    error_count = 0
    if editor_context is not None:
        error_count += editor_context.error_count
    if terminal_context is not None:
        error_count += len(terminal_context.detected_errors)
    # Normalize: 0 errors = 0, 5+ errors = 1.0
    scores.append(min(1.0, error_count / 5.0))

    # 2. Visual clutter (visible lines in editor)
    if editor_context is not None:
        visible_lines = editor_context.visible_lines
        # 50 lines = low, 200+ = high
        scores.append(min(1.0, visible_lines / 200.0))
    else:
        scores.append(0.0)

    # 3. Tab overload
    if browser_context is not None:
        tab_count = browser_context.tab_count
        # 5 tabs = low, 20+ = high
        scores.append(min(1.0, tab_count / 20.0))
    else:
        scores.append(0.0)

    # 4. Window thrashing (switch rate)
    # 10/min = moderate, 30+/min = high
    scores.append(min(1.0, window_switch_rate / 30.0))

    # 5. Code block size
    if editor_context is not None and editor_context.visible_code:
        code_lines = editor_context.visible_code.count("\n") + 1
        scores.append(min(1.0, code_lines / 150.0))
    else:
        scores.append(0.0)

    # 6. Diagnostic severity (weighted)
    if editor_context is not None:
        weighted = (
            editor_context.error_count * 1.0
            + editor_context.warning_count * 0.3
        )
        scores.append(min(1.0, weighted / 5.0))
    else:
        scores.append(0.0)

    # 7. Document density (browser content)
    if browser_context is not None and browser_context.active_tab_content_excerpt:
        content_len = len(browser_context.active_tab_content_excerpt)
        scores.append(min(1.0, content_len / 4000.0))
    else:
        scores.append(0.0)

    if not scores:
        return 0.0

    return float(np.clip(np.mean(scores), 0.0, 1.0))
