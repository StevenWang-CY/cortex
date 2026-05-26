"""P0 §3.12 — OS notification routing on INTERVENTION_TRIGGER.

The daemon stamps ``desktop_not_focused`` on the wire when its focus
probe reports the dashboard isn't the active window. The browser
extension + VS Code surfaces fan that into OS-level cues
(chrome.notifications, status-bar pulse). The macOS helper is import-
safe on non-mac so the module itself must always be importable.
"""

from __future__ import annotations

import sys

import pytest

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    UIPlan,
)
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import WebSocketServer


def _build_plan() -> InterventionPlan:
    return InterventionPlan(
        intervention_id="iv_test",
        level="overlay_only",
        headline="Close 4 tabs",
        situation_summary="Six tabs open, only one is your code",
        primary_focus="your editor",
        micro_steps=[
            MicroStep(text="close the noise"),
            MicroStep(text="breathe"),
        ],
        hide_targets=[],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="overlay_only",
            max_visible_lines=40,
        ),
        tone="supportive",
        suggested_actions=[],
        causal_explanation="Tab thrashing detected",
        consent_level="suggest",
        plan_warnings=[],
    )


def test_make_intervention_stamps_desktop_not_focused_when_false() -> None:
    server = WebSocketServer()
    msg = server._make_intervention_trigger(_build_plan(), desktop_focused=False)
    assert msg.payload["desktop_not_focused"] is True


def test_make_intervention_omits_flag_when_focused() -> None:
    server = WebSocketServer()
    msg = server._make_intervention_trigger(_build_plan(), desktop_focused=True)
    assert "desktop_not_focused" not in msg.payload


def test_make_intervention_omits_flag_when_unknown() -> None:
    server = WebSocketServer()
    msg = server._make_intervention_trigger(_build_plan(), desktop_focused=None)
    assert "desktop_not_focused" not in msg.payload


def test_macos_notifications_helper_is_importable() -> None:
    # Module must be importable on every platform — the helper degrades
    # to a no-op when PyObjC / non-mac.
    import cortex.libs.utils.macos_notifications as mn  # noqa: WPS433

    assert hasattr(mn, "send_intervention_notification")
    assert hasattr(mn, "send_notification")


def test_macos_notifications_noop_on_non_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.libs.utils import macos_notifications as mn

    monkeypatch.setattr(mn, "_is_macos", lambda: False)
    # Always returns False (no notification dispatched).
    assert mn.send_intervention_notification(title="T", body="B") is False
    assert mn.send_notification(title="T", body="B") is False


def test_message_type_includes_os_notification_messages() -> None:
    assert MessageType.START_FOCUS_AUTO.value == "START_FOCUS_AUTO"
    assert MessageType.STOP_FOCUS_AUTO.value == "STOP_FOCUS_AUTO"
    assert MessageType.QUIET_MODE_STATE.value == "QUIET_MODE_STATE"
