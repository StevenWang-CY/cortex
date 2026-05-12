"""
Unit tests for Context Engine — App classifier, adapters, assembler, complexity.

Tests verify:
- App classification from window titles
- Tab type classification from URLs
- Workspace mode detection
- Terminal error detection (Python tracebacks, shell errors)
- Editor adapter payload parsing
- Browser adapter payload parsing + tab classification
- Complexity scoring
- Context assembly
- Module imports
"""

from __future__ import annotations

from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TerminalContext,
)
from cortex.services.context_engine.app_classifier import (
    classify_app,
    classify_mode,
    classify_tab_type,
)
from cortex.services.context_engine.assembler import (
    ContextAssembler,
    compute_complexity_score,
)
from cortex.services.context_engine.browser_adapter import BrowserAdapter
from cortex.services.context_engine.editor_adapter import EditorAdapter
from cortex.services.context_engine.terminal_adapter import TerminalAdapter

# =============================================================================
# App Classifier Tests
# =============================================================================


class TestClassifyApp:
    """Test app classification from window titles."""

    def test_vscode(self):
        assert classify_app("Visual Studio Code") == "vscode"
        assert classify_app("Code") == "vscode"
        assert classify_app("Cursor") == "vscode"

    def test_chrome(self):
        assert classify_app("Google Chrome") == "chrome"
        assert classify_app("Firefox") == "chrome"  # Browser category
        assert classify_app("Arc") == "chrome"

    def test_terminal(self):
        assert classify_app("Terminal") == "terminal"
        assert classify_app("iTerm2") == "terminal"  # matches 'iterm'
        assert classify_app("Alacritty") == "terminal"
        assert classify_app("Warp") == "terminal"

    def test_other(self):
        assert classify_app("Spotify") == "other"
        assert classify_app("Finder") == "other"
        assert classify_app(None) == "other"


class TestClassifyTabType:
    """Test tab type classification from URLs."""

    def test_stackoverflow(self):
        assert classify_tab_type("https://stackoverflow.com/questions/12345") == "stackoverflow"
        assert classify_tab_type("https://math.stackexchange.com/q/123") == "stackoverflow"

    def test_documentation(self):
        assert classify_tab_type("https://developer.mozilla.org/en-US/docs") == "documentation"
        assert classify_tab_type("https://react.dev/learn") == "documentation"
        assert classify_tab_type("https://docs.python.org/3/library") == "documentation"
        assert classify_tab_type("https://readthedocs.io/en/latest") == "documentation"

    def test_search(self):
        assert classify_tab_type("https://www.google.com/search?q=test") == "search"
        assert classify_tab_type("https://duckduckgo.com/?q=test") == "search"

    def test_code_host(self):
        assert classify_tab_type("https://github.com/user/repo") == "code_host"
        assert classify_tab_type("https://gitlab.com/user/repo") == "code_host"

    def test_social(self):
        assert classify_tab_type("https://twitter.com/user") == "social"
        assert classify_tab_type("https://www.reddit.com/r/python") == "social"

    def test_video_platform(self):
        assert classify_tab_type("https://youtube.com/watch?v=123") == "video_platform"

    def test_ai_assistant(self):
        assert classify_tab_type("https://gemini.google.com/app") == "ai_assistant"
        assert classify_tab_type("https://chatgpt.com/c/123") == "ai_assistant"

    def test_communication(self):
        assert classify_tab_type("https://app.slack.com/client") == "communication"
        assert classify_tab_type("https://discord.com/channels/123") == "communication"

    def test_other(self):
        assert classify_tab_type("https://example.com") == "other"
        assert classify_tab_type("https://myapp.io/dashboard") == "other"


class TestClassifyMode:
    """Test workspace mode detection."""

    def test_coding_with_vscode(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 50),
            diagnostics=[Diagnostic(severity="error", message="err", line=1)],
        )
        assert classify_mode("vscode", editor_context=editor) == "coding_debugging"

    def test_coding_without_errors(self):
        editor = EditorContext(file_path="/test.py", visible_range=(1, 50))
        assert classify_mode("vscode", editor_context=editor) == "coding_debugging"

    def test_terminal_errors(self):
        terminal = TerminalContext(detected_errors=["KeyError: 'key'"])
        assert classify_mode("terminal", terminal_context=terminal) == "terminal_errors"

    def test_reading_docs(self):
        browser = BrowserContext(
            active_tab_title="Docs", active_tab_url="https://docs.python.org",
            all_tabs=[
                TabInfo(title="Docs", url="https://docs.python.org", tab_type="documentation"),
                TabInfo(title="MDN", url="https://developer.mozilla.org", tab_type="documentation"),
                TabInfo(title="Other", url="https://example.com", tab_type="other"),
            ],
            tab_type_classification={"documentation": 2, "other": 1},
        )
        assert classify_mode("chrome", browser_context=browser) == "reading_docs"

    def test_browsing(self):
        browser = BrowserContext(
            active_tab_title="Reddit", active_tab_url="https://reddit.com",
            all_tabs=[
                TabInfo(title="Reddit", url="https://reddit.com", tab_type="social"),
                TabInfo(title="SO", url="https://stackoverflow.com", tab_type="stackoverflow"),
            ],
            tab_type_classification={"social": 1, "stackoverflow": 1},
        )
        assert classify_mode("chrome", browser_context=browser) == "browsing"

    def test_mixed_fallback(self):
        assert classify_mode("other") == "mixed"


# =============================================================================
# Terminal Adapter Tests
# =============================================================================


class TestTerminalAdapter:
    """Test terminal error detection and context building."""

    def test_detect_python_traceback(self):
        adapter = TerminalAdapter()
        adapter.feed_lines([
            "$ python test.py",
            "Traceback (most recent call last):",
            '  File "test.py", line 47, in parse_config',
            "    config['api_key']",
            "KeyError: 'api_key'",
            "",
        ])
        ctx = adapter.get_context()
        assert len(ctx.detected_errors) >= 1
        assert "KeyError" in ctx.detected_errors[0]

    def test_detect_shell_error(self):
        adapter = TerminalAdapter()
        adapter.feed_lines([
            "$ npm start",
            "error: ENOENT: no such file or directory, open 'package.json'",
        ])
        ctx = adapter.get_context()
        assert len(ctx.detected_errors) >= 1
        assert "no such file or directory" in ctx.detected_errors[0].lower()

    def test_detect_command_not_found(self):
        adapter = TerminalAdapter()
        adapter.feed_lines([
            "$ foo",
            "bash: foo: command not found",
        ])
        ctx = adapter.get_context()
        assert len(ctx.detected_errors) >= 1

    def test_repeated_commands(self):
        adapter = TerminalAdapter()
        adapter.feed_lines([
            "$ python test.py",
            "Error occurred",
            "$ python test.py",
            "Error occurred",
            "$ python test.py",
        ])
        ctx = adapter.get_context()
        assert "python test.py" in ctx.repeated_commands

    def test_running_command(self):
        adapter = TerminalAdapter()
        adapter.set_running_command("python server.py")
        ctx = adapter.get_context()
        assert ctx.running_command == "python server.py"

    def test_empty_history(self):
        adapter = TerminalAdapter()
        ctx = adapter.get_context()
        assert ctx.last_n_lines == []
        assert ctx.detected_errors == []

    def test_last_n_lines_limit(self):
        adapter = TerminalAdapter()
        adapter.feed_lines([f"line {i}" for i in range(100)])
        ctx = adapter.get_context(last_n=10)
        assert len(ctx.last_n_lines) == 10

    def test_reset(self):
        adapter = TerminalAdapter()
        adapter.feed_lines(["$ test", "error: failed"])
        adapter.set_running_command("test")
        adapter.reset()
        ctx = adapter.get_context()
        assert ctx.last_n_lines == []
        assert ctx.running_command is None


# =============================================================================
# Editor Adapter Tests
# =============================================================================


class TestEditorAdapter:
    """Test editor adapter payload parsing."""

    def test_parse_editor_context(self):
        payload = {
            "file_path": "/src/main.py",
            "visible_range": [1, 100],
            "symbol_at_cursor": "parse_config",
            "diagnostics": [
                {"severity": "error", "message": "NameError", "line": 42, "column": 5},
                {"severity": "warning", "message": "unused var", "line": 10},
            ],
            "recent_edits": ["Added import"],
            "visible_code": "def parse_config():\n    pass",
        }
        adapter = EditorAdapter()
        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert ctx.file_path == "/src/main.py"
        assert ctx.symbol_at_cursor == "parse_config"
        assert ctx.error_count == 1
        assert ctx.warning_count == 1
        assert adapter.available

    def test_parse_minimal_payload(self):
        adapter = EditorAdapter()
        ctx = adapter.update_from_payload({})
        assert ctx is not None
        assert ctx.file_path == ""
        assert ctx.diagnostics == []

    def test_unavailable_without_ws(self):
        adapter = EditorAdapter()
        assert not adapter.available


# =============================================================================
# Browser Adapter Tests
# =============================================================================


class TestBrowserAdapter:
    """Test browser adapter payload parsing and tab classification."""

    def test_parse_browser_context(self):
        payload = {
            "active_tab_title": "Python Docs",
            "active_tab_url": "https://docs.python.org/3",
            "active_tab_content_excerpt": "Python standard library...",
            "all_tabs": [
                {"title": "Python Docs", "url": "https://docs.python.org/3", "is_active": True},
                {"title": "SO Question", "url": "https://stackoverflow.com/q/123"},
                {"title": "GitHub", "url": "https://github.com/user/repo"},
            ],
        }
        adapter = BrowserAdapter()
        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert ctx.active_tab_title == "Python Docs"
        assert ctx.tab_count == 3
        # Tab types should be auto-classified
        assert ctx.tab_type_classification.get("documentation", 0) >= 1
        assert ctx.tab_type_classification.get("stackoverflow", 0) >= 1
        assert ctx.tab_type_classification.get("code_host", 0) >= 1
        assert adapter.available

    def test_content_truncation(self):
        payload = {
            "active_tab_title": "Long page",
            "active_tab_url": "https://example.com",
            "active_tab_content_excerpt": "x" * 10000,
            "all_tabs": [],
        }
        adapter = BrowserAdapter()
        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert len(ctx.active_tab_content_excerpt) <= 8000

    def test_empty_payload(self):
        adapter = BrowserAdapter()
        ctx = adapter.update_from_payload({})
        assert ctx is not None
        assert ctx.tab_count == 0

    def test_unavailable_without_ws(self):
        adapter = BrowserAdapter()
        assert not adapter.available


# =============================================================================
# Complexity Score Tests
# =============================================================================


class TestComplexityScore:
    """Test workspace complexity scoring."""

    def test_zero_complexity_no_context(self):
        score = compute_complexity_score()
        assert score == 0.0

    def test_high_complexity_many_errors(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 200),
            visible_code="\n".join(["code"] * 200),
            diagnostics=[
                Diagnostic(severity="error", message=f"err{i}", line=i + 1)
                for i in range(5)
            ],
        )
        terminal = TerminalContext(
            detected_errors=["Error 1", "Error 2", "Error 3"],
        )
        browser = BrowserContext(
            active_tab_title="SO", active_tab_url="https://stackoverflow.com",
            active_tab_content_excerpt="x" * 5000,
            all_tabs=[TabInfo(title=f"tab{i}", url=f"https://example.com/{i}")
                       for i in range(20)],
        )
        score = compute_complexity_score(
            editor_context=editor,
            browser_context=browser,
            terminal_context=terminal,
            window_switch_rate=30.0,
        )
        assert score > 0.6

    def test_low_complexity_simple_workspace(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 30),
            visible_code="\n".join(["code"] * 30),
        )
        score = compute_complexity_score(
            editor_context=editor,
            window_switch_rate=2.0,
        )
        assert score < 0.3

    def test_complexity_range(self):
        """Complexity should always be in [0, 1]."""
        for errors in range(10):
            editor = EditorContext(
                file_path="/test.py", visible_range=(1, 50),
                diagnostics=[
                    Diagnostic(severity="error", message="err", line=i + 1)
                    for i in range(errors)
                ],
            )
            score = compute_complexity_score(editor_context=editor)
            assert 0.0 <= score <= 1.0


# =============================================================================
# Context Assembler Tests
# =============================================================================


class TestContextAssembler:
    """Test context assembly and mode detection."""

    def test_sync_assembly_coding(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 50),
            diagnostics=[Diagnostic(severity="error", message="err", line=1)],
        )
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            editor_context=editor,
            active_app="vscode",
        )
        assert ctx.mode == "coding_debugging"
        assert ctx.active_app == "vscode"
        assert ctx.has_editor
        assert 0.0 <= ctx.complexity_score <= 1.0

    def test_sync_assembly_terminal_errors(self):
        terminal = TerminalContext(detected_errors=["KeyError: 'key'"])
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            terminal_context=terminal,
            active_app="terminal",
        )
        assert ctx.mode == "terminal_errors"
        assert ctx.has_terminal

    def test_sync_assembly_browsing(self):
        browser = BrowserContext(
            active_tab_title="Test", active_tab_url="https://example.com",
            all_tabs=[
                TabInfo(title="SO", url="https://stackoverflow.com", tab_type="stackoverflow"),
                TabInfo(title="GH", url="https://github.com", tab_type="code_host"),
            ],
            tab_type_classification={"stackoverflow": 1, "code_host": 1},
        )
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            browser_context=browser,
            active_app="chrome",
        )
        assert ctx.mode == "browsing"
        assert ctx.has_browser

    def test_goal_inference_from_error(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 50),
            diagnostics=[Diagnostic(severity="error", message="NameError: x", line=10)],
        )
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            editor_context=editor, active_app="vscode",
        )
        assert ctx.current_goal_hint is not None
        assert "NameError" in ctx.current_goal_hint

    def test_goal_inference_from_symbol(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 50),
            symbol_at_cursor="parse_config",
        )
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            editor_context=editor, active_app="vscode",
        )
        assert "parse_config" in (ctx.current_goal_hint or "")

    def test_window_switch_rate_affects_complexity(self):
        editor = EditorContext(file_path="/test.py", visible_range=(1, 50))
        assembler_low = ContextAssembler(window_switch_rate=2.0)
        assembler_high = ContextAssembler(window_switch_rate=30.0)

        ctx_low = assembler_low.build_context_sync(
            editor_context=editor, active_app="vscode",
        )
        ctx_high = assembler_high.build_context_sync(
            editor_context=editor, active_app="vscode",
        )
        assert ctx_high.complexity_score > ctx_low.complexity_score

    def test_to_llm_context_string(self):
        editor = EditorContext(
            file_path="/test.py", visible_range=(1, 50),
            diagnostics=[Diagnostic(severity="error", message="err", line=1)],
            visible_code="def main(): pass",
        )
        assembler = ContextAssembler()
        ctx = assembler.build_context_sync(
            editor_context=editor, active_app="vscode",
        )
        llm_ctx = ctx.to_llm_context()
        assert "coding_debugging" in llm_ctx
        assert "vscode" in llm_ctx
        assert "/test.py" in llm_ctx


# =============================================================================
# Module Import Tests
# =============================================================================


class TestContextEngineImports:
    """Test that all context engine exports are importable."""

    def test_import_classifier(self):
        from cortex.services.context_engine import classify_app, classify_mode, classify_tab_type
        assert classify_app is not None
        assert classify_mode is not None
        assert classify_tab_type is not None

    def test_import_adapters(self):
        from cortex.services.context_engine import BrowserAdapter, EditorAdapter, TerminalAdapter
        assert EditorAdapter is not None
        assert BrowserAdapter is not None
        assert TerminalAdapter is not None

    def test_import_assembler(self):
        from cortex.services.context_engine import ContextAssembler, compute_complexity_score
        assert ContextAssembler is not None
        assert compute_complexity_score is not None
