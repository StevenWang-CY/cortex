"""
Unit tests for LeetCodeAdapter.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from cortex.libs.adapters.base import CortexAdapter
from cortex.libs.adapters.leetcode_adapter import LeetCodeAdapter
from cortex.libs.schemas.leetcode import LeetCodeContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter_with_sender():
    """Return (adapter, sent_messages) with a mock ws_sender wired up."""
    sent_messages: list[dict[str, Any]] = []

    async def mock_sender(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    adapter = LeetCodeAdapter()
    adapter.set_ws_sender(mock_sender)
    return adapter, sent_messages


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLeetCodeAdapterProtocol:
    def test_isinstance_cortex_adapter(self):
        """LeetCodeAdapter must satisfy the CortexAdapter protocol."""
        adapter = LeetCodeAdapter()
        assert isinstance(adapter, CortexAdapter)


class TestLeetCodeAdapterProperties:
    def test_name_returns_leetcode(self):
        adapter = LeetCodeAdapter()
        assert adapter.name == "leetcode"

    def test_capabilities_include_expected_actions(self):
        adapter = LeetCodeAdapter()
        caps = adapter.capabilities
        expected = [
            "lock_editor",
            "intercept_submit",
            "gate_solutions",
            "show_scratchpad",
            "show_pattern_ladder",
            "show_lockout",
            "show_consolidation",
            "show_submission_gate",
            "show_solution_friction",
        ]
        for action in expected:
            assert action in caps, f"Missing capability: {action}"


class TestLeetCodeAdapterExecute:
    @pytest.mark.asyncio
    async def test_execute_returns_error_without_ws_sender(self):
        adapter = LeetCodeAdapter()
        result = await adapter.execute("lock_editor", {"duration": 90})
        assert result.success is False
        assert result.error is not None
        assert "WebSocket sender not configured" in result.error

    @pytest.mark.asyncio
    async def test_execute_sends_correct_message_format(self):
        adapter, sent = _make_adapter_with_sender()

        result = await adapter.execute("show_scratchpad", {"problem_id": "42"})

        assert result.success is True
        assert len(sent) == 1
        assert sent[0] == {
            "type": "LEETCODE_SHOW_SCRATCHPAD",
            "payload": {"problem_id": "42"},
        }
        assert result.data == {"action": "show_scratchpad", "params": {"problem_id": "42"}}

    @pytest.mark.asyncio
    async def test_execute_rejects_unknown_action(self):
        adapter, sent = _make_adapter_with_sender()
        result = await adapter.execute("hack_leetcode", {"payload": "evil"})
        assert result.success is False
        assert "Unknown action" in result.error
        assert len(sent) == 0


class TestLeetCodeAdapterContext:
    def test_update_context_caches_leetcode_context(self):
        adapter = LeetCodeAdapter()
        data = {
            "problem_id": "121",
            "title": "Best Time to Buy and Sell Stock",
            "difficulty": "Easy",
            "tags": ["Array", "Dynamic Programming"],
            "stage": "IMPLEMENT",
        }
        adapter.update_context(data)

        ctx = adapter.context
        assert isinstance(ctx, LeetCodeContext)
        assert ctx.problem_id == "121"
        assert ctx.title == "Best Time to Buy and Sell Stock"
        assert ctx.difficulty == "Easy"
        assert ctx.stage.value == "IMPLEMENT"


class TestLeetCodeAdapterHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_false_without_sender(self):
        adapter = LeetCodeAdapter()
        assert await adapter.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_true_with_sender_and_recent_context(self):
        adapter, _ = _make_adapter_with_sender()
        adapter.update_context({"title": "Two Sum"})
        # update_context sets _last_context_update to time.time(), so within 5s
        assert await adapter.health_check() is True
