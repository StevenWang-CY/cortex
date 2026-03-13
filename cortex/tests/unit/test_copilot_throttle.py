"""Tests for CopilotThrottle — AI assistant throttling on cognitive overload."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from cortex.services.throttle.copilot_throttle import CopilotThrottle


class TestCopilotThrottle:
    def setup_method(self):
        self.ws_server = MagicMock()
        self.ws_server.send_to_client = AsyncMock()
        self.throttle = CopilotThrottle(
            ws_server=self.ws_server,
            hyper_threshold=0.85,
            flow_threshold=0.70,
        )

    @pytest.mark.asyncio
    async def test_throttle_on_hyper(self):
        """HYPER with high confidence → throttle."""
        changed = await self.throttle.on_state_change("HYPER", 0.9)
        assert changed is True
        assert self.throttle.is_throttled is True
        self.ws_server.send_to_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_unthrottle_on_flow(self):
        """FLOW after HYPER → unthrottle."""
        await self.throttle.on_state_change("HYPER", 0.9)
        changed = await self.throttle.on_state_change("FLOW", 0.8)
        assert changed is True
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_no_change_below_threshold(self):
        """HYPER below threshold → no throttle."""
        changed = await self.throttle.on_state_change("HYPER", 0.5)
        assert changed is False
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_no_redundant_throttle(self):
        """Already throttled → no change on repeated HYPER."""
        await self.throttle.on_state_change("HYPER", 0.9)
        changed = await self.throttle.on_state_change("HYPER", 0.95)
        assert changed is False

    @pytest.mark.asyncio
    async def test_disabled_no_action(self):
        """When disabled, no throttling happens."""
        self.throttle.enabled = False
        changed = await self.throttle.on_state_change("HYPER", 0.95)
        assert changed is False
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_disable_while_throttled_reenables(self):
        await self.throttle.on_state_change("HYPER", 0.9)
        assert self.throttle.is_throttled is True
        self.throttle.enabled = False
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_force_enable(self):
        await self.throttle.on_state_change("HYPER", 0.9)
        await self.throttle.force_enable()
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_no_ws_server_no_error(self):
        """Without ws_server, methods should not raise."""
        throttle = CopilotThrottle(ws_server=None)
        changed = await throttle.on_state_change("HYPER", 0.9)
        assert changed is True
        assert throttle.is_throttled is True

    @pytest.mark.asyncio
    async def test_flow_below_threshold_no_unthrottle(self):
        await self.throttle.on_state_change("HYPER", 0.9)
        changed = await self.throttle.on_state_change("FLOW", 0.3)
        assert changed is False
        assert self.throttle.is_throttled is True
