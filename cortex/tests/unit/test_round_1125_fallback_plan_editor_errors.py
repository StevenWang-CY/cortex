"""The rule-based plan must keep its editor-error guidance when tabs are open.

``build_fallback_plan`` is the plan every user sees in the default
``no_llm`` mode. Its browser branch keeps an error-driven plan only when
``summary.startswith("Error")``. The terminal branch's summary
("Error detected: ...") passes that guard, but the editor branch's summary
("3 error(s) in /src/main.py") does not, so with any browser tab open the
editor-error headline and summary were replaced by "Focus on <active browser
tab title>" and "N tabs open, 0 appear distracting".
"""

from __future__ import annotations

from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
)
from cortex.services.llm_engine.client import build_fallback_plan


def _editor_with_errors() -> EditorContext:
    return EditorContext(
        file_path="/src/main.py",
        visible_range=(1, 50),
        symbol_at_cursor="handle_request",
        diagnostics=[
            Diagnostic(severity="error", message="NameError: x is not defined", line=10),
            Diagnostic(severity="error", message="TypeError: bad operand", line=14),
        ],
        visible_code="def handle_request():\n    return x",
    )


def _browser(active_title: str) -> BrowserContext:
    tabs = [
        TabInfo(title=active_title, url="https://www.youtube.com/watch?v=1", tab_type="other",
                is_active=True),
        TabInfo(title="Docs", url="https://docs.python.org/3/", tab_type="documentation"),
    ]
    return BrowserContext(
        active_tab_title=active_title,
        active_tab_url="https://www.youtube.com/watch?v=1",
        all_tabs=tabs,
    )


def test_editor_errors_survive_open_browser_tabs() -> None:
    without_browser = build_fallback_plan(
        TaskContext(mode="coding_debugging", active_app="vscode", complexity_score=0.7,
                    editor_context=_editor_with_errors())
    )
    with_browser = build_fallback_plan(
        TaskContext(mode="coding_debugging", active_app="vscode", complexity_score=0.7,
                    editor_context=_editor_with_errors(),
                    browser_context=_browser("Lo-fi beats to relax to"))
    )

    assert without_browser.headline == "Resolve editor errors one at a time"
    assert with_browser.headline == without_browser.headline
    assert "error" in with_browser.situation_summary.lower()
    # Tab recommendations are still produced alongside the error guidance.
    assert with_browser.tab_recommendations is not None
