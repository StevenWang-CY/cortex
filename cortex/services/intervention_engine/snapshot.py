"""
Intervention Engine — Workspace Snapshot

Captures the pre-intervention state of the user's workspace so that
everything can be restored after the intervention ends.
"""

from __future__ import annotations

import time

from cortex.libs.schemas.context import BrowserContext, EditorContext, TaskContext
from cortex.libs.schemas.intervention import (
    FoldState,
    TabVisibility,
    WorkspaceSnapshot,
    generate_intervention_id,
)


def capture_snapshot(
    context: TaskContext | None = None,
    intervention_id: str | None = None,
    *,
    timestamp: float | None = None,
) -> WorkspaceSnapshot:
    """
    Capture a snapshot of the current workspace state.

    Args:
        context: Current workspace context. If None, creates an empty snapshot.
        intervention_id: Unique ID for this intervention.
        timestamp: Override timestamp for testing.

    Returns:
        A WorkspaceSnapshot that can be used to restore the workspace.
    """
    if intervention_id is None:
        intervention_id = generate_intervention_id()
    if timestamp is None:
        timestamp = time.monotonic()

    fold_states: list[FoldState] = []
    editor_visible_range: tuple[int, int] | None = None
    tab_visibility: list[TabVisibility] = []
    active_tab_id: str | None = None
    terminal_scroll_position: int | None = None

    if context is not None:
        # Capture editor state
        if context.editor_context is not None:
            fold_states = _capture_editor_folds(context.editor_context)
            editor_visible_range = context.editor_context.visible_range

        # Capture browser state
        if context.browser_context is not None:
            tab_visibility, active_tab_id = _capture_browser_tabs(
                context.browser_context
            )

        # Capture terminal state
        if context.terminal_context is not None:
            terminal_scroll_position = len(
                context.terminal_context.last_n_lines
            )

    return WorkspaceSnapshot(
        intervention_id=intervention_id,
        timestamp=timestamp,
        fold_states=fold_states,
        editor_visible_range=editor_visible_range,
        tab_visibility=tab_visibility,
        active_tab_id=active_tab_id,
        terminal_scroll_position=terminal_scroll_position,
    )


def _capture_editor_folds(editor: EditorContext) -> list[FoldState]:
    """
    Capture current editor fold state.

    Since we don't have actual fold info from the editor adapter yet,
    we capture the file path and visible range as a baseline.
    The VS Code extension will provide real fold ranges via WebSocket.
    """
    return [
        FoldState(
            file_path=editor.file_path,
            folded_ranges=[],  # Will be populated by VS Code extension
        )
    ]


def _capture_browser_tabs(
    browser: BrowserContext,
) -> tuple[list[TabVisibility], str | None]:
    """Capture browser tab visibility state."""
    tabs: list[TabVisibility] = []
    active_id: str | None = None

    for i, tab in enumerate(browser.all_tabs):
        tab_id = f"tab_{i}"
        is_active = tab.is_active
        tabs.append(
            TabVisibility(
                tab_id=tab_id,
                url=tab.url,
                was_visible=True,  # All tabs visible before intervention
                was_active=is_active,
            )
        )
        if is_active:
            active_id = tab_id

    return tabs, active_id
