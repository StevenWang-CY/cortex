"""Audit-prod G4 — ``dispatch_action_to_browser`` boundary tests.

The desktop overlay's action buttons call this daemon method to forward
their click to the connected browser extension. The pre-fix code:
- accepted any ``intervention_id`` (stale clicks after dismiss reached
  the browser),
- accepted any ``action`` dict (malformed actions reached executeAction's
  ``default`` arm),
- returned 0 silently when no browser was connected (caller had no
  telemetry to surface).

Plus the cross-client confused-deputy gate: ``request_dispatch=True``
must only be honoured for desktop-originated USER_ACTIONs; a browser
client trying to forge it is rejected.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.services.runtime_daemon import CortexDaemon


class _RecordingWS:
    """Stand-in WS server. Records send_message calls; returns the
    configurable ``recipients`` count for the next call."""

    def __init__(self, recipients: int = 1) -> None:
        self.calls: list[tuple[str, dict[str, Any], list[str] | None]] = []
        self.recipients = recipients

    async def send_message(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        target_client_types: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> int:
        self.calls.append((message_type, payload, target_client_types))
        return self.recipients

    def connected_client_types(self) -> list[str]:
        return ["chrome"] if self.recipients > 0 else []


@pytest.fixture
def daemon() -> CortexDaemon:
    d = CortexDaemon.__new__(CortexDaemon)
    d._ws_server = _RecordingWS(recipients=1)
    d._active_intervention_id = "iv_active"
    d._recorder = type("_R", (), {"append": lambda *_a, **_k: None})()
    return d


def _valid_action() -> dict[str, Any]:
    return {
        "action_id": "act-123",
        "action_type": "close_tab",
        "label": "Close noisy tab",
        "reason": "Detected high context-switching",
        "tab_index": 2,
    }


def test_dispatch_succeeds_for_active_intervention(daemon: CortexDaemon) -> None:
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent == 1
    assert len(daemon._ws_server.calls) == 1
    msg_type, payload, targets = daemon._ws_server.calls[0]
    assert msg_type == "ACTION_DISPATCH"
    assert payload["intervention_id"] == "iv_active"
    assert payload["action"]["action_id"] == "act-123"
    assert targets == ["chrome", "edge"]


def test_dispatch_rejected_for_stale_intervention(daemon: CortexDaemon) -> None:
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_OLD", _valid_action())
    )
    assert sent == 0
    assert daemon._ws_server.calls == []


def test_dispatch_rejected_while_pending(daemon: CortexDaemon) -> None:
    daemon._active_intervention_id = "__pending__"
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent == 0


def test_dispatch_rejected_when_no_active(daemon: CortexDaemon) -> None:
    daemon._active_intervention_id = None
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent == 0


def test_dispatch_rejected_for_invalid_action(daemon: CortexDaemon) -> None:
    bad = {"action_type": "definitely_not_a_real_type"}  # missing action_id; bad type
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", bad)
    )
    assert sent == 0


def test_dispatch_returns_zero_when_no_browser_connected(
    daemon: CortexDaemon,
) -> None:
    daemon._ws_server.recipients = 0  # type: ignore[attr-defined]
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent == 0
    # send_message was still invoked (no-target case is server-side).
    assert len(daemon._ws_server.calls) == 1


def test_handle_user_action_blocks_non_desktop_request_dispatch(
    daemon: CortexDaemon,
) -> None:
    """A chrome client cannot forge ACTION_DISPATCH for its peers."""
    daemon._helpfulness = type("_H", (), {"record_rating": lambda *_a, **_k: None})()
    payload = {
        "action_id": "x",
        "action_type": "close_tab",
        "intervention_id": "iv_active",
        "request_dispatch": True,
        "_source_client_type": "chrome",  # NOT desktop
    }
    asyncio.run(daemon._handle_user_action(payload))
    # No ACTION_DISPATCH should have been forwarded.
    assert daemon._ws_server.calls == []


def test_handle_user_action_honours_desktop_request_dispatch(
    daemon: CortexDaemon,
) -> None:
    daemon._helpfulness = type("_H", (), {"record_rating": lambda *_a, **_k: None})()
    payload = {
        "action_id": "y",
        "action_type": "close_tab",
        "intervention_id": "iv_active",
        "request_dispatch": True,
        "_source_client_type": "desktop",
        "action": _valid_action(),
    }
    asyncio.run(daemon._handle_user_action(payload))
    assert any(
        call[0] == "ACTION_DISPATCH"
        for call in daemon._ws_server.calls
    )


def test_dispatch_succeeds_before_engagement_clears_active_id(
    daemon: CortexDaemon,
) -> None:
    """Regression guard for the ordering bug Phase 3 caught: the
    desktop-overlay flow must dispatch BEFORE the engaged USER_ACTION
    runs through ``_restore_manager.engage`` (which clears
    ``_active_intervention_id``). If dispatch ran second it would
    always be rejected as stale. This test asserts that running
    dispatch-then-engage in that order succeeds; the inverse fails.
    """
    daemon._helpfulness = type("_H", (), {"record_rating": lambda *_a, **_k: None})()
    # dispatch first — should reach the browser.
    sent = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent == 1
    # Simulate engagement clearing the active id (as restore_manager.engage does).
    daemon._active_intervention_id = None
    # A second dispatch attempt now correctly rejects (stale).
    sent2 = asyncio.run(
        daemon.dispatch_action_to_browser("iv_active", _valid_action())
    )
    assert sent2 == 0
