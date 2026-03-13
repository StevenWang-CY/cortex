"""Tests for CopilotThrottle state-change behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cortex.services.throttle.copilot_throttle import CopilotThrottle


class TestOnStateChange:
    """CopilotThrottle should disable on HYPER and re-enable on FLOW."""

    @pytest.mark.asyncio
    async def test_flow_to_hyper_disables(self):
        """Transition to HYPER with high confidence should throttle (disable suggestions)."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws)

        assert not throttle.is_throttled
        changed = await throttle.on_state_change("HYPER", confidence=0.90)

        assert changed is True
        assert throttle.is_throttled
        ws.send_to_client.assert_called_once()
        call_kwargs = ws.send_to_client.call_args
        assert call_kwargs.kwargs["payload"]["command"] == "cortex.disableInlineSuggestions"

    @pytest.mark.asyncio
    async def test_hyper_to_flow_enables(self):
        """Transition from HYPER back to FLOW should un-throttle (enable suggestions)."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws)

        # First go into HYPER
        await throttle.on_state_change("HYPER", confidence=0.90)
        ws.reset_mock()

        # Then recover to FLOW
        changed = await throttle.on_state_change("FLOW", confidence=0.80)
        assert changed is True
        assert not throttle.is_throttled
        ws.send_to_client.assert_called_once()
        call_kwargs = ws.send_to_client.call_args
        assert call_kwargs.kwargs["payload"]["command"] == "cortex.enableInlineSuggestions"

    @pytest.mark.asyncio
    async def test_redundant_hyper_does_not_resend(self):
        """Repeated HYPER calls should not send the disable command again."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws)

        await throttle.on_state_change("HYPER", confidence=0.90)
        ws.reset_mock()

        changed = await throttle.on_state_change("HYPER", confidence=0.95)
        assert changed is False
        ws.send_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_redundant_flow_does_not_resend(self):
        """Repeated FLOW calls when already un-throttled should not send enable."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws)

        # Already in un-throttled state (default)
        changed = await throttle.on_state_change("FLOW", confidence=0.90)
        assert changed is False
        ws.send_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_hyper_ignored(self):
        """HYPER with confidence below threshold should not throttle."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws, hyper_threshold=0.85)

        changed = await throttle.on_state_change("HYPER", confidence=0.50)
        assert changed is False
        assert not throttle.is_throttled
        ws.send_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_flow_ignored(self):
        """FLOW with confidence below threshold should not un-throttle."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws, flow_threshold=0.70)

        # Throttle first
        await throttle.on_state_change("HYPER", confidence=0.90)
        ws.reset_mock()

        changed = await throttle.on_state_change("FLOW", confidence=0.50)
        assert changed is False
        assert throttle.is_throttled

    @pytest.mark.asyncio
    async def test_disabled_feature_does_nothing(self):
        """When enabled=False, no state changes should occur."""
        ws = AsyncMock()
        throttle = CopilotThrottle(ws_server=ws)
        throttle.enabled = False

        changed = await throttle.on_state_change("HYPER", confidence=0.95)
        assert changed is False
        assert not throttle.is_throttled
        ws.send_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_ws_server_still_tracks_state(self):
        """With no ws_server, throttle state should still update."""
        throttle = CopilotThrottle(ws_server=None)

        changed = await throttle.on_state_change("HYPER", confidence=0.90)
        assert changed is True
        assert throttle.is_throttled
