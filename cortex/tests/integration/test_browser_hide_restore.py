"""
Integration Test — Browser Tab Hide/Restore Round-Trip

Tests the tab hide and restore workflow as it flows through the Cortex system:

1. Daemon sends INTERVENTION_TRIGGER with hide_targets
2. Chrome extension receives trigger, groups non-active tabs
3. User dismisses → sends USER_ACTION(dismissed)
4. Daemon receives dismissal → extension restores tab visibility

Since the actual Chrome extension runs in a separate browser process,
this test validates the Python-side protocol: WebSocket message
serialization, BrowserContext parsing, tab classification, snapshot
creation, and restore manager integration.
"""

from __future__ import annotations

import json
import time

from cortex.libs.schemas.context import BrowserContext, TabInfo
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    TabVisibility,
    UIPlan,
    WorkspaceSnapshot,
)
from cortex.services.api_gateway.websocket_server import WebSocketServer, WSMessage
from cortex.services.context_engine.app_classifier import classify_tab_type
from cortex.services.context_engine.browser_adapter import BrowserAdapter
from cortex.services.intervention_engine.restore import RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTabClassification:
    """Test URL-based tab type classification."""

    def test_stackoverflow(self) -> None:
        assert classify_tab_type("https://stackoverflow.com/questions/123") == "stackoverflow"

    def test_stackexchange(self) -> None:
        assert classify_tab_type("https://unix.stackexchange.com/q/456") == "stackoverflow"

    def test_documentation(self) -> None:
        assert classify_tab_type("https://docs.python.org/3/library/") == "documentation"
        assert classify_tab_type("https://developer.mozilla.org/en-US/docs/") == "documentation"
        assert classify_tab_type("https://react.dev/reference/react") == "documentation"

    def test_search(self) -> None:
        assert classify_tab_type("https://www.google.com/search?q=python") == "search"
        assert classify_tab_type("https://duckduckgo.com/?q=rust") == "search"

    def test_code_host(self) -> None:
        assert classify_tab_type("https://github.com/user/repo") == "code_host"
        assert classify_tab_type("https://gitlab.com/group/project") == "code_host"

    def test_social(self) -> None:
        assert classify_tab_type("https://twitter.com/user") == "social"
        assert classify_tab_type("https://www.reddit.com/r/python") == "social"

    def test_video_platform(self) -> None:
        assert classify_tab_type("https://www.youtube.com/watch?v=abc") == "video_platform"

    def test_ai_assistant(self) -> None:
        assert classify_tab_type("https://gemini.google.com/app") == "ai_assistant"
        assert classify_tab_type("https://chatgpt.com/c/123") == "ai_assistant"

    def test_communication(self) -> None:
        assert classify_tab_type("https://app.slack.com/client") == "communication"
        assert classify_tab_type("https://discord.com/channels/123") == "communication"

    def test_other(self) -> None:
        assert classify_tab_type("https://example.com/page") == "other"
        assert classify_tab_type("https://mysite.io/dashboard") == "other"


class TestBrowserContextParsing:
    """Test BrowserAdapter parsing of context payloads."""

    def test_parse_full_context(self) -> None:
        """BrowserAdapter correctly parses a full context payload."""
        adapter = BrowserAdapter()

        payload = {
            "active_tab_title": "Python docs",
            "active_tab_url": "https://docs.python.org/3/",
            "active_tab_content_excerpt": "Python 3.12 documentation...",
            "all_tabs": [
                {
                    "title": "Python docs",
                    "url": "https://docs.python.org/3/",
                    "is_active": True,
                },
                {
                    "title": "SO: Python async",
                    "url": "https://stackoverflow.com/questions/123",
                    "is_active": False,
                },
                {
                    "title": "GitHub - myrepo",
                    "url": "https://github.com/user/repo",
                    "is_active": False,
                },
            ],
        }

        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert ctx.active_tab_title == "Python docs"
        assert ctx.tab_count == 3
        assert ctx.tab_type_classification.get("documentation", 0) >= 1
        assert ctx.tab_type_classification.get("stackoverflow", 0) >= 1
        assert ctx.tab_type_classification.get("code_host", 0) >= 1

    def test_parse_empty_tabs(self) -> None:
        """BrowserAdapter handles empty tab list."""
        adapter = BrowserAdapter()
        payload = {
            "active_tab_title": "",
            "active_tab_url": "",
            "all_tabs": [],
        }
        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert ctx.tab_count == 0

    def test_content_excerpt_truncation(self) -> None:
        """BrowserAdapter truncates content to ~2000 tokens."""
        adapter = BrowserAdapter()
        long_content = "x" * 10000
        payload = {
            "active_tab_title": "Test",
            "active_tab_url": "https://example.com",
            "active_tab_content_excerpt": long_content,
            "all_tabs": [],
        }
        ctx = adapter.update_from_payload(payload)
        assert ctx is not None
        assert len(ctx.active_tab_content_excerpt) <= 8000


class TestHideRestoreProtocol:
    """Test the WebSocket message protocol for tab hide/restore."""

    def test_intervention_with_hide_targets(self) -> None:
        """INTERVENTION_TRIGGER correctly includes hide_targets."""
        plan = InterventionPlan(
            level="simplified_workspace",
            situation_summary="Too many tabs open, scattered attention.",
            headline="Simplify your workspace",
            primary_focus="Focus on the active documentation",
            micro_steps=["Close unrelated tabs", "Read the error message"],
            hide_targets=["browser_tabs_except_active"],
            ui_plan=UIPlan(
                dim_background=True,
                show_overlay=True,
                intervention_type="simplified_workspace",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)

        assert msg.type == "INTERVENTION_TRIGGER"
        assert "browser_tabs_except_active" in msg.payload["hide_targets"]
        assert msg.payload["level"] == "simplified_workspace"

    def test_intervention_without_hide(self) -> None:
        """overlay_only intervention has no hide_targets."""
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Mild stress detected.",
            headline="Take a moment",
            primary_focus="Breathe",
            micro_steps=["Inhale 4s, hold 7s, exhale 8s"],
            hide_targets=[],
            ui_plan=UIPlan(
                show_overlay=True,
                intervention_type="overlay_only",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)

        assert msg.payload["hide_targets"] == []

    def test_user_action_dismissed(self) -> None:
        """USER_ACTION(dismissed) message format for tab restore trigger."""
        raw = json.dumps({
            "type": "USER_ACTION",
            "payload": {
                "action": "dismissed",
                "intervention_id": "int_tab_123",
                "timestamp": time.monotonic(),
            },
            "timestamp": time.monotonic(),
            "sequence": 10,
        })

        msg = WSMessage.from_json(raw)
        assert msg.type == "USER_ACTION"
        assert msg.payload["action"] == "dismissed"
        assert msg.payload["intervention_id"] == "int_tab_123"


class TestTabSnapshotRoundTrip:
    """Test workspace snapshot with browser tab state."""

    def test_snapshot_with_tab_visibility(self) -> None:
        """WorkspaceSnapshot captures tab visibility state."""
        snapshot = WorkspaceSnapshot(
            intervention_id="int_tabs_test",
            timestamp=time.monotonic(),
            tab_visibility=[
                TabVisibility(
                    tab_id="tab_0",
                    url="https://docs.python.org/3/",
                    was_visible=True,
                    was_active=True,
                ),
                TabVisibility(
                    tab_id="tab_1",
                    url="https://stackoverflow.com/q/123",
                    was_visible=True,
                    was_active=False,
                ),
                TabVisibility(
                    tab_id="tab_2",
                    url="https://reddit.com/r/python",
                    was_visible=True,
                    was_active=False,
                ),
            ],
            active_tab_id="tab_0",
        )

        assert snapshot.has_browser_state
        assert len(snapshot.tab_visibility) == 3
        assert snapshot.active_tab_id == "tab_0"

        # Find active tab
        active = next(t for t in snapshot.tab_visibility if t.was_active)
        assert active.url == "https://docs.python.org/3/"

    def test_snapshot_without_browser_state(self) -> None:
        """WorkspaceSnapshot without browser state reports correctly."""
        snapshot = WorkspaceSnapshot(
            intervention_id="int_nobrowser",
            timestamp=time.monotonic(),
        )
        assert not snapshot.has_browser_state

    def test_snapshot_serialization_round_trip(self) -> None:
        """Tab visibility data survives serialization round-trip."""
        original = WorkspaceSnapshot(
            intervention_id="int_serial_tabs",
            timestamp=1234567890.0,
            tab_visibility=[
                TabVisibility(
                    tab_id="t1",
                    url="https://example.com",
                    was_visible=True,
                    was_active=True,
                ),
                TabVisibility(
                    tab_id="t2",
                    url="https://other.com",
                    was_visible=True,
                    was_active=False,
                ),
            ],
            active_tab_id="t1",
        )

        data = original.model_dump()
        restored = WorkspaceSnapshot(**data)

        assert len(restored.tab_visibility) == 2
        assert restored.active_tab_id == "t1"
        assert restored.tab_visibility[0].url == "https://example.com"
        assert restored.tab_visibility[1].was_active is False


class TestRestoreManagerBrowserIntegration:
    """Test RestoreManager with browser tab state."""

    def test_start_and_retrieve_intervention(self) -> None:
        """RestoreManager stores browser tab state for intervention."""
        manager = RestoreManager()

        snapshot = WorkspaceSnapshot(
            intervention_id="int_restore_test",
            timestamp=time.monotonic(),
            tab_visibility=[
                TabVisibility(
                    tab_id="tab_active",
                    url="https://docs.python.org",
                    was_visible=True,
                    was_active=True,
                ),
                TabVisibility(
                    tab_id="tab_hidden",
                    url="https://reddit.com",
                    was_visible=True,
                    was_active=False,
                ),
            ],
            active_tab_id="tab_active",
        )

        manager.start_intervention(
            "int_restore_test", snapshot, started_at=time.monotonic(),
        )

        active = manager.get_active("int_restore_test")
        assert active is not None
        assert active.snapshot.has_browser_state
        assert len(active.snapshot.tab_visibility) == 2

    def test_missing_intervention(self) -> None:
        """RestoreManager returns None for unknown intervention IDs."""
        manager = RestoreManager()
        assert manager.get_active("nonexistent") is None


class TestCaptureSnapshotWithBrowser:
    """Test capture_snapshot function with browser context."""

    def test_capture_with_browser_context(self) -> None:
        """capture_snapshot generates tab visibility from BrowserContext."""
        from cortex.libs.schemas.context import TaskContext

        browser_ctx = BrowserContext(
            active_tab_title="Python Docs",
            active_tab_url="https://docs.python.org",
            all_tabs=[
                TabInfo(
                    title="Python Docs",
                    url="https://docs.python.org",
                    tab_type="documentation",
                    is_active=True,
                ),
                TabInfo(
                    title="GitHub",
                    url="https://github.com/user/repo",
                    tab_type="code_host",
                    is_active=False,
                ),
            ],
        )

        task_ctx = TaskContext(
            mode="reading_docs",
            active_app="chrome",
            complexity_score=0.5,
            browser_context=browser_ctx,
        )

        snapshot = capture_snapshot(
            context=task_ctx,
            intervention_id="int_capture_browser",
        )

        assert snapshot.has_browser_state
        assert len(snapshot.tab_visibility) == 2
        assert any(t.url == "https://docs.python.org" for t in snapshot.tab_visibility)

    def test_capture_without_browser(self) -> None:
        """capture_snapshot without browser context has no tab state."""
        snapshot = capture_snapshot(context=None, intervention_id="int_no_browser")
        assert not snapshot.has_browser_state


class TestFullHideRestoreWorkflow:
    """End-to-end test of the tab hide/restore workflow data flow."""

    def test_complete_workflow(self) -> None:
        """
        Full hide/restore round-trip:
        1. Create intervention plan with browser_tabs_except_active
        2. Serialize to INTERVENTION_TRIGGER
        3. Capture workspace snapshot with tab visibility
        4. Simulate user dismissal
        5. Verify snapshot enables restoration
        """
        # Step 1: Create intervention plan
        plan = InterventionPlan(
            level="simplified_workspace",
            situation_summary="12 tabs open, attention scattered across docs and social.",
            headline="Focus on one thing",
            primary_focus="Read the Python asyncio docs",
            micro_steps=[
                "Set aside social media tabs",
                "Read the current docs page",
            ],
            hide_targets=["browser_tabs_except_active"],
            ui_plan=UIPlan(
                dim_background=True,
                show_overlay=True,
                intervention_type="simplified_workspace",
            ),
        )

        assert plan.is_valid
        assert not plan.is_destructive

        # Step 2: Serialize
        server = WebSocketServer()
        trigger_msg = server._make_intervention_trigger(plan)
        trigger_json = trigger_msg.to_json()
        parsed = json.loads(trigger_json)

        assert parsed["type"] == "INTERVENTION_TRIGGER"
        assert "browser_tabs_except_active" in parsed["payload"]["hide_targets"]

        # Step 3: Capture snapshot with tab state
        snapshot = WorkspaceSnapshot(
            intervention_id=plan.intervention_id,
            timestamp=time.monotonic(),
            tab_visibility=[
                TabVisibility(
                    tab_id="tab_0",
                    url="https://docs.python.org/3/library/asyncio.html",
                    was_visible=True,
                    was_active=True,
                ),
                TabVisibility(
                    tab_id="tab_1",
                    url="https://stackoverflow.com/questions/async",
                    was_visible=True,
                    was_active=False,
                ),
                TabVisibility(
                    tab_id="tab_2",
                    url="https://reddit.com/r/python",
                    was_visible=True,
                    was_active=False,
                ),
                TabVisibility(
                    tab_id="tab_3",
                    url="https://youtube.com/watch?v=xyz",
                    was_visible=True,
                    was_active=False,
                ),
            ],
            active_tab_id="tab_0",
        )

        restore_mgr = RestoreManager()
        now = time.monotonic()
        restore_mgr.start_intervention(
            plan.intervention_id, snapshot, started_at=now,
        )

        # Step 4: Simulate user dismissal
        dismiss_msg = WSMessage(
            type="USER_ACTION",
            payload={
                "action": "dismissed",
                "intervention_id": plan.intervention_id,
            },
        )
        assert dismiss_msg.payload["action"] == "dismissed"

        # Step 5: Retrieve snapshot for restoration
        active = restore_mgr.get_active(plan.intervention_id)
        assert active is not None
        assert active.snapshot.has_browser_state
        assert len(active.snapshot.tab_visibility) == 4
        assert active.snapshot.active_tab_id == "tab_0"

        # Verify all hidden tabs are recorded
        hidden = [t for t in active.snapshot.tab_visibility if not t.was_active]
        assert len(hidden) == 3
        hidden_urls = {t.url for t in hidden}
        assert "https://reddit.com/r/python" in hidden_urls
        assert "https://youtube.com/watch?v=xyz" in hidden_urls

    def test_overlay_only_no_hide(self) -> None:
        """Overlay-only intervention does not hide tabs."""
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Elevated stress.",
            headline="Breathe",
            primary_focus="Take a deep breath",
            micro_steps=["4-7-8 breathing"],
            hide_targets=[],
            ui_plan=UIPlan(
                show_overlay=True,
                intervention_type="overlay_only",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)
        assert msg.payload["hide_targets"] == []

        snapshot = WorkspaceSnapshot(
            intervention_id=plan.intervention_id,
            timestamp=time.monotonic(),
        )
        assert not snapshot.has_browser_state
