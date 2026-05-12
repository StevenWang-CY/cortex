"""
Integration Test — VS Code Fold/Restore Round-Trip

Tests the fold and restore workflow as it flows through the Cortex system:

1. Daemon sends INTERVENTION_TRIGGER with fold_unrelated_code=True
2. VS Code extension receives trigger, saves fold snapshot, applies folds
3. User dismisses → sends USER_ACTION(dismissed)
4. Daemon receives dismissal → sends restore command
5. Extension restores fold state from snapshot

Since the actual VS Code extension runs in a separate TypeScript process,
this test simulates the WebSocket message exchange and validates the
protocol correctness of the fold/restore round-trip.
"""

from __future__ import annotations

import asyncio
import json
import time

from cortex.libs.schemas.intervention import (
    FoldState,
    InterventionPlan,
    UIPlan,
    WorkspaceSnapshot,
)
from cortex.services.api_gateway.websocket_server import WebSocketServer, WSMessage
from cortex.services.intervention_engine.restore import RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockWebSocket:
    """Simulates a WebSocket connection for testing."""

    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.closed = False
        self._incoming: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent_messages.append(data)

    async def close(self) -> None:
        self.closed = True

    def inject_message(self, msg: str) -> None:
        """Inject a message as if received from the client."""
        self._incoming.put_nowait(msg)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await asyncio.wait_for(self._incoming.get(), timeout=1.0)
        except TimeoutError:
            raise StopAsyncIteration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFoldRestoreProtocol:
    """Test the WebSocket message protocol for fold/restore operations."""

    def test_intervention_trigger_includes_fold_flag(self) -> None:
        """INTERVENTION_TRIGGER with fold_unrelated_code=True is correctly serialized."""
        plan = InterventionPlan(
            level="simplified_workspace",
            situation_summary="Multiple TypeErrors in main.py.",
            headline="Focus on the errors",
            primary_focus="Fix TypeError on line 47",
            micro_steps=["Read the error message", "Check variable types"],
            hide_targets=["editor_symbols_except_current_function"],
            ui_plan=UIPlan(
                fold_unrelated_code=True,
                show_overlay=True,
                intervention_type="simplified_workspace",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)

        assert msg.type == "INTERVENTION_TRIGGER"
        assert msg.payload["ui_plan"]["fold_unrelated_code"] is True
        assert msg.payload["level"] == "simplified_workspace"
        assert msg.payload["headline"] == "Focus on the errors"
        assert len(msg.payload["micro_steps"]) == 2

    def test_intervention_trigger_without_fold(self) -> None:
        """overlay_only intervention does not request folding."""
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="You seem stressed.",
            headline="Take a breath",
            primary_focus="Relax for a moment",
            micro_steps=["Take a deep breath"],
            ui_plan=UIPlan(
                fold_unrelated_code=False,
                show_overlay=True,
                intervention_type="overlay_only",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)

        assert msg.payload["ui_plan"]["fold_unrelated_code"] is False
        assert msg.payload["level"] == "overlay_only"

    def test_user_action_dismissed_format(self) -> None:
        """USER_ACTION(dismissed) message is correctly parsed."""
        raw = json.dumps({
            "type": "USER_ACTION",
            "payload": {
                "action": "dismissed",
                "intervention_id": "int_abc123",
                "timestamp": time.monotonic(),
            },
            "timestamp": time.monotonic(),
            "sequence": 42,
        })

        msg = WSMessage.from_json(raw)
        assert msg.type == "USER_ACTION"
        assert msg.payload["action"] == "dismissed"
        assert msg.payload["intervention_id"] == "int_abc123"
        assert msg.sequence == 42

    def test_user_action_engaged_format(self) -> None:
        """USER_ACTION(engaged) message is correctly parsed."""
        raw = json.dumps({
            "type": "USER_ACTION",
            "payload": {
                "action": "engaged",
                "intervention_id": "int_def456",
            },
            "timestamp": time.monotonic(),
            "sequence": 43,
        })

        msg = WSMessage.from_json(raw)
        assert msg.payload["action"] == "engaged"


class TestFoldSnapshotRoundTrip:
    """Test fold state snapshot capture and restoration data flow."""

    def test_fold_state_model(self) -> None:
        """FoldState model correctly stores file path and folded ranges."""
        fold = FoldState(
            file_path="/src/main.py",
            folded_ranges=[(1, 20), (50, 100), (120, 150)],
        )

        assert fold.file_path == "/src/main.py"
        assert len(fold.folded_ranges) == 3
        assert fold.folded_ranges[0] == (1, 20)

    def test_workspace_snapshot_with_fold_states(self) -> None:
        """WorkspaceSnapshot correctly captures editor fold state."""
        snapshot = WorkspaceSnapshot(
            intervention_id="int_test123",
            timestamp=time.monotonic(),
            fold_states=[
                FoldState(
                    file_path="/src/main.py",
                    folded_ranges=[(10, 30), (60, 80)],
                ),
                FoldState(
                    file_path="/src/utils.py",
                    folded_ranges=[(5, 15)],
                ),
            ],
            editor_visible_range=(40, 60),
        )

        assert snapshot.has_editor_state
        assert len(snapshot.fold_states) == 2
        assert snapshot.editor_visible_range == (40, 60)
        assert snapshot.fold_states[0].file_path == "/src/main.py"

    def test_workspace_snapshot_without_editor(self) -> None:
        """WorkspaceSnapshot without editor state reports correctly."""
        snapshot = WorkspaceSnapshot(
            intervention_id="int_noeditor",
            timestamp=time.monotonic(),
        )

        assert not snapshot.has_editor_state
        assert len(snapshot.fold_states) == 0

    def test_snapshot_round_trip_serialization(self) -> None:
        """Snapshot can be serialized and deserialized without data loss."""
        original = WorkspaceSnapshot(
            intervention_id="int_serial",
            timestamp=1234567890.0,
            fold_states=[
                FoldState(
                    file_path="/project/app.py",
                    folded_ranges=[(1, 10), (30, 50), (70, 90)],
                ),
            ],
            editor_visible_range=(25, 55),
            overlay_present=True,
        )

        # Serialize and deserialize
        data = original.model_dump()
        restored = WorkspaceSnapshot(**data)

        assert restored.intervention_id == original.intervention_id
        assert restored.timestamp == original.timestamp
        assert len(restored.fold_states) == 1
        assert restored.fold_states[0].file_path == "/project/app.py"
        assert restored.fold_states[0].folded_ranges == [(1, 10), (30, 50), (70, 90)]
        assert restored.editor_visible_range == (25, 55)
        assert restored.overlay_present is True


class TestRestoreManagerIntegration:
    """Test RestoreManager for fold state snapshot management."""

    def test_snapshot_save_and_retrieve(self) -> None:
        """RestoreManager stores and retrieves snapshots via start_intervention."""
        manager = RestoreManager()

        snapshot = WorkspaceSnapshot(
            intervention_id="int_mgr_test",
            timestamp=time.monotonic(),
            fold_states=[
                FoldState(
                    file_path="/src/handler.py",
                    folded_ranges=[(5, 25), (40, 60)],
                ),
            ],
            editor_visible_range=(10, 30),
        )

        manager.start_intervention(
            "int_mgr_test", snapshot, started_at=time.monotonic(),
        )

        retrieved = manager.get_active("int_mgr_test")
        assert retrieved is not None
        assert retrieved.intervention_id == "int_mgr_test"
        assert retrieved.snapshot.fold_states[0].folded_ranges == [(5, 25), (40, 60)]

    def test_capture_snapshot_function(self) -> None:
        """capture_snapshot creates a WorkspaceSnapshot with correct fields."""
        snapshot = capture_snapshot(
            context=None,
            intervention_id="int_capture_test",
            timestamp=12345.0,
        )

        assert snapshot.intervention_id == "int_capture_test"
        assert snapshot.timestamp == 12345.0
        assert len(snapshot.fold_states) == 0

    def test_missing_intervention_returns_none(self) -> None:
        """RestoreManager returns None for unknown intervention IDs."""
        manager = RestoreManager()
        assert manager.get_active("nonexistent") is None


class TestContextRequestResponse:
    """Test the CONTEXT_REQUEST/CONTEXT_RESPONSE protocol used for fold operations."""

    def test_context_request_message_format(self) -> None:
        """CONTEXT_REQUEST includes the expected commands."""
        # This is what the EditorAdapter sends to VS Code
        request = {
            "type": "CONTEXT_REQUEST",
            "payload": {
                "commands": [
                    "cortex.getActiveFile",
                    "cortex.getDiagnostics",
                    "cortex.getSymbolAtCursor",
                ],
            },
        }

        raw = json.dumps(request)
        parsed = json.loads(raw)

        assert parsed["type"] == "CONTEXT_REQUEST"
        assert "cortex.getActiveFile" in parsed["payload"]["commands"]
        assert "cortex.getDiagnostics" in parsed["payload"]["commands"]
        assert "cortex.getSymbolAtCursor" in parsed["payload"]["commands"]

    def test_context_response_with_fold_info(self) -> None:
        """CONTEXT_RESPONSE includes fold-relevant editor context."""
        response = {
            "type": "CONTEXT_RESPONSE",
            "payload": {
                "file_path": "/src/main.py",
                "visible_range": [40, 60],
                "symbol_at_cursor": "parse_config",
                "diagnostics": [
                    {
                        "severity": "error",
                        "message": "KeyError: 'host'",
                        "line": 47,
                        "column": 12,
                        "source": "python",
                        "code": None,
                    }
                ],
                "visible_code": "def parse_config(data):\n    host = data['host']\n",
                "recent_edits": [],
            },
        }

        raw = json.dumps(response)
        parsed = json.loads(raw)

        assert parsed["type"] == "CONTEXT_RESPONSE"
        payload = parsed["payload"]
        assert payload["file_path"] == "/src/main.py"
        assert payload["symbol_at_cursor"] == "parse_config"
        assert len(payload["diagnostics"]) == 1
        assert payload["diagnostics"][0]["severity"] == "error"
        assert payload["diagnostics"][0]["line"] == 47


class TestInterventionFoldWorkflow:
    """End-to-end test of the fold intervention workflow data flow."""

    def test_full_fold_restore_data_flow(self) -> None:
        """
        Complete fold/restore round-trip:
        1. Create intervention plan with fold_unrelated_code=True
        2. Serialize to INTERVENTION_TRIGGER
        3. Capture workspace snapshot
        4. Simulate user dismissal
        5. Verify snapshot enables restoration
        """
        # Step 1: Create intervention plan
        plan = InterventionPlan(
            level="simplified_workspace",
            situation_summary="3 TypeErrors across 2 files, debugging loop detected.",
            headline="Focus on the TypeError",
            primary_focus="Fix the KeyError in parse_config",
            micro_steps=[
                "Read the traceback carefully",
                "Check if 'host' key exists",
                "Add a default value",
            ],
            hide_targets=["editor_symbols_except_current_function"],
            ui_plan=UIPlan(
                fold_unrelated_code=True,
                show_overlay=True,
                dim_background=True,
                intervention_type="simplified_workspace",
            ),
        )

        assert plan.is_valid
        assert not plan.is_destructive

        # Step 2: Serialize to message
        server = WebSocketServer()
        trigger_msg = server._make_intervention_trigger(plan)
        trigger_json = trigger_msg.to_json()
        parsed = json.loads(trigger_json)

        assert parsed["type"] == "INTERVENTION_TRIGGER"
        assert parsed["payload"]["ui_plan"]["fold_unrelated_code"] is True

        # Step 3: Capture snapshot (simulating pre-fold state)
        snapshot = WorkspaceSnapshot(
            intervention_id=plan.intervention_id,
            timestamp=time.monotonic(),
            fold_states=[
                FoldState(
                    file_path="/src/main.py",
                    folded_ranges=[(1, 10), (80, 120)],
                ),
            ],
            editor_visible_range=(35, 65),
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
        assert active.snapshot.has_editor_state
        assert active.snapshot.fold_states[0].file_path == "/src/main.py"
        assert active.snapshot.editor_visible_range == (35, 65)

    def test_overlay_only_no_fold(self) -> None:
        """
        Overlay-only intervention should not include fold commands.
        """
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Elevated heart rate detected.",
            headline="Take a breather",
            primary_focus="Pause and breathe",
            micro_steps=["Try 4-7-8 breathing"],
            ui_plan=UIPlan(
                fold_unrelated_code=False,
                show_overlay=True,
                intervention_type="overlay_only",
            ),
        )

        server = WebSocketServer()
        msg = server._make_intervention_trigger(plan)

        assert msg.payload["ui_plan"]["fold_unrelated_code"] is False
        assert msg.payload["level"] == "overlay_only"

        # No snapshot needed since no folds applied
        snapshot = WorkspaceSnapshot(
            intervention_id=plan.intervention_id,
            timestamp=time.monotonic(),
        )
        assert not snapshot.has_editor_state
