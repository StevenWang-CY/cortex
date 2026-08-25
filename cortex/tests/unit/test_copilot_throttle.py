"""Tests for fail-closed containment of legacy Copilot throttling."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.services.throttle.copilot_throttle import CopilotThrottle


class TestCopilotThrottle:
    def setup_method(self):
        self.ws_server = MagicMock()
        # D.5: throttle now emits a COPILOT_THROTTLE message via
        # ``send_message(type, payload, target_client_types=...)`` so the
        # VS Code extension's existing handler can pick it up. The legacy
        # ``send_to_client`` shim has been removed.
        self.ws_server.send_message = AsyncMock()
        self.throttle = CopilotThrottle(
            ws_server=self.ws_server,
            hyper_threshold=0.85,
            flow_threshold=0.70,
        )

    @pytest.mark.asyncio
    async def test_throttle_on_hyper(self):
        """A state estimate is not authority to edit VS Code settings."""
        changed = await self.throttle.on_state_change("HYPER", 0.9)
        assert changed is False
        assert self.throttle.is_throttled is False
        self.ws_server.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unthrottle_on_flow(self):
        """No synthetic release is needed when no mutation was owned."""
        await self.throttle.on_state_change("HYPER", 0.9)
        changed = await self.throttle.on_state_change("FLOW", 0.8)
        assert changed is False
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
        assert self.throttle.is_throttled is False
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
        assert changed is False
        assert throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_flow_below_threshold_no_unthrottle(self):
        await self.throttle.on_state_change("HYPER", 0.9)
        changed = await self.throttle.on_state_change("FLOW", 0.3)
        assert changed is False
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_unthrottle_on_hyper_to_recovery(self):
        """HYPER → RECOVERY remains mutation-free end to end."""
        await self.throttle.on_state_change("HYPER", 0.9)
        assert self.throttle.is_throttled is False

        changed = await self.throttle.on_state_change("RECOVERY", 0.6)
        assert changed is False
        assert self.throttle.is_throttled is False
        self.ws_server.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unthrottle_on_hyper_to_hypo(self):
        """FLOW → HYPER → HYPO cannot change editor configuration."""
        await self.throttle.on_state_change("HYPER", 0.9)
        assert self.throttle.is_throttled is False

        changed = await self.throttle.on_state_change("HYPO", 0.4)
        assert changed is False
        assert self.throttle.is_throttled is False

    @pytest.mark.asyncio
    async def test_no_unthrottle_when_not_throttled(self):
        """A non-HYPER state when already enabled must not toggle anything."""
        changed = await self.throttle.on_state_change("RECOVERY", 0.6)
        assert changed is False
        assert self.throttle.is_throttled is False
        self.ws_server.send_message.assert_not_called()
