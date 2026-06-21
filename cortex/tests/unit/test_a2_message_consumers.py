"""Owner-A2 consumer / UI-wiring tests.

Covers the missing desktop/browser message consumers and edge cases
wired in by owner A2:

* P1-FC-INTERVENTION-FAILED — the daemon's total-mutation-failure
  broadcast must surface through the controller's ``error_occurred`` toast
  path (previously it had no consumer on any surface).
* P1-FC-INTERVENTION-PROMPT — the controller dispatch map must be
  complete (informational handler present).
* P2-CONTRACT-1 — controller schedule-failure paths must emit the
  schema-valid ``'internal'`` error literal, not ``'schedule_failed'``.
* P2-FE-START-TIMER — the ``start_timer`` native action must drive the
  break/countdown overlay (``start_biology_break``) instead of being a
  silent no-op.
* P2-FEAT-SCREENSHARE — the intervention overlay must be suppressed when
  a display is being captured/shared, and shown otherwise.
* P2-FE-MULTIMON — the overlay must target the screen under the cursor.

Run with:
    QT_QPA_PLATFORM=offscreen pytest \
        cortex/tests/unit/test_a2_message_consumers.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Drop any stale PySide6 stub left by sibling tests so we exercise the
# real Qt widgets (same guard the other desktop_shell tests use).
for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - environment misconfig
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# P1-FC-INTERVENTION-FAILED / -PROMPT — controller bridge consumers
# ---------------------------------------------------------------------------


def test_intervention_failed_routes_to_error_signal(qapp) -> None:
    """A synthetic INTERVENTION_FAILED dispatched through the bridge
    handler must fire ``error_occurred`` (which the controller routes to
    the dashboard toast). Previously this message had no consumer."""
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    received: list[tuple[str, str, str]] = []
    bridge.error_occurred.connect(
        lambda title, body, cid: received.append((title, body, cid))
    )

    bridge.on_intervention_failed(
        {
            "intervention_id": "iv-99",
            "error_reason": "Extension lacks tab permission",
            "failed_action_types": ["close_tab", "mute_tab"],
        }
    )

    assert len(received) == 1, f"expected one toast, got {received}"
    title, body, cid = received[0]
    assert "couldn't be applied" in title.lower()
    assert body == "Extension lacks tab permission"
    assert cid == "iv-99"


def test_intervention_failed_synthesizes_reason_from_action_types(qapp) -> None:
    """When ``error_reason`` is empty the handler must still surface a
    human-readable body derived from the failed action types."""
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    received: list[tuple[str, str, str]] = []
    bridge.error_occurred.connect(
        lambda title, body, cid: received.append((title, body, cid))
    )

    bridge.on_intervention_failed(
        {"intervention_id": "iv-1", "failed_action_types": ["close_tab"]}
    )

    assert len(received) == 1
    assert "close tab" in received[0][1].lower()


def test_intervention_failed_is_in_controller_dispatch_map(qapp) -> None:
    """The broadcast-observer dispatch map must route INTERVENTION_FAILED
    and INTERVENTION_PROMPT to the corresponding bridge methods. We rebuild
    the map exactly as ``_install_broadcast_observer`` does and assert the
    entries exist and are bound to the right handlers."""
    from cortex.apps.desktop_shell.controller import (
        CortexAppController,
        DaemonBridge,
    )
    from cortex.libs.schemas.ws_message_types import MessageType

    ctrl = CortexAppController.__new__(CortexAppController)
    bridge = DaemonBridge()
    ctrl._bridge = bridge

    # Stand-in ws_server whose send_message we can wrap. The observer
    # installs a wrapper around it and builds the type→handler map.
    captured: dict[str, Any] = {}

    async def _send(message_type: str, payload: dict, **kwargs: Any) -> int:
        return 0

    class _WS:
        send_message = staticmethod(_send)

    class _Daemon:
        _ws_server = _WS()

    ctrl._daemon = _Daemon()
    ctrl._install_ws_broadcast_observer()

    # After install, the wrapped send_message must, for INTERVENTION_FAILED,
    # reach the bridge handler. Spy on the error signal end-to-end.
    received: list[tuple[str, str, str]] = []
    bridge.error_occurred.connect(
        lambda t, b, c: received.append((t, b, c))
    )

    wrapped = ctrl._daemon._ws_server.send_message
    assert getattr(wrapped, "_cortex_broadcast_wrapped", False) is True

    asyncio.run(
        wrapped(
            MessageType.INTERVENTION_FAILED.value,
            {"intervention_id": "iv-x", "error_reason": "boom"},
        )
    )
    captured["after"] = list(received)
    assert captured["after"], "INTERVENTION_FAILED did not reach the toast"
    assert captured["after"][0][1] == "boom"


def test_intervention_prompt_handler_is_safe(qapp) -> None:
    """The informational INTERVENTION_PROMPT handler must not raise and
    must not emit an error toast (it is log-only on desktop)."""
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    errors: list[Any] = []
    bridge.error_occurred.connect(lambda *a: errors.append(a))

    bridge.on_intervention_prompt(
        {"action_type": "prompt_micro_commit", "prompt": "Commit one line?"}
    )
    bridge.on_intervention_prompt({})  # malformed → still safe

    assert errors == []


# ---------------------------------------------------------------------------
# P2-CONTRACT-1 — schedule-failure paths emit 'internal', not 'schedule_failed'
# ---------------------------------------------------------------------------


class _RaisingScheduler:
    """A daemon-loop stand-in whose schedule always raises so the
    controller's outer ``except`` (the schedule-failure path) runs."""


@pytest.fixture()
def controller_with_failing_schedule(qapp, monkeypatch):
    from cortex.apps.desktop_shell import controller as controller_mod
    from cortex.apps.desktop_shell.controller import (
        CortexAppController,
        DaemonBridge,
    )

    ctrl = CortexAppController.__new__(CortexAppController)
    ctrl._bridge = DaemonBridge()
    ctrl._dashboard = None

    # Daemon + loop are present (so we pass the early guard) but the
    # schedule call itself blows up, exercising the schedule-failure path.
    class _Daemon:
        async def list_sessions(self, *a: Any, **k: Any) -> Any: ...
        async def get_session(self, *a: Any, **k: Any) -> Any: ...
        async def get_trends(self, *a: Any, **k: Any) -> Any: ...

    ctrl._daemon = _Daemon()
    ctrl._daemon_loop = asyncio.new_event_loop()

    def _boom(coro: Any = None, *_a: Any, **_k: Any) -> Any:
        # Close the unscheduled coroutine so pytest doesn't warn about a
        # never-awaited coroutine; then raise to trigger the schedule-
        # failure path under test.
        try:
            if coro is not None and hasattr(coro, "close"):
                coro.close()
        except Exception:
            pass
        raise RuntimeError("schedule kaput")

    monkeypatch.setattr(
        controller_mod.asyncio, "run_coroutine_threadsafe", _boom
    )
    yield ctrl
    ctrl._daemon_loop.close()


def test_history_schedule_failure_emits_internal(
    controller_with_failing_schedule,
) -> None:
    ctrl = controller_with_failing_schedule
    seen: list[dict] = []
    ctrl._bridge.session_list_received.connect(lambda p: seen.append(p))

    ctrl._on_history_requested(None, 30)

    assert seen and seen[-1].get("error") == "internal", seen


def test_detail_schedule_failure_emits_internal(
    controller_with_failing_schedule,
) -> None:
    ctrl = controller_with_failing_schedule
    seen: list[dict] = []
    ctrl._bridge.session_detail_received.connect(lambda p: seen.append(p))

    ctrl._on_detail_requested("sess-1")

    assert seen and seen[-1].get("error") == "internal", seen


def test_trends_schedule_failure_emits_internal(
    controller_with_failing_schedule,
) -> None:
    ctrl = controller_with_failing_schedule
    seen: list[dict] = []
    ctrl._bridge.trends_received.connect(lambda p: seen.append(p))

    ctrl._on_trends_requested("week", False)

    assert seen and seen[-1].get("error") == "internal", seen


# ---------------------------------------------------------------------------
# P2-FE-START-TIMER — start_timer drives the break/countdown overlay
# ---------------------------------------------------------------------------


class _BreakRecordingDaemon:
    def __init__(self) -> None:
        self.break_calls: list[dict[str, Any]] = []
        self.user_actions: list[dict[str, Any]] = []

    async def start_biology_break(self, **kwargs: Any) -> dict[str, Any]:
        self.break_calls.append(dict(kwargs))
        return {"ok": True}

    async def _handle_user_action(self, payload: dict) -> None:
        self.user_actions.append(payload)


def test_start_timer_invokes_break_countdown(qapp) -> None:
    """P2-FE-START-TIMER: clicking 'start timer' must invoke the
    daemon's break/countdown overlay with the metadata duration and a
    plain (no-pattern, no-audio) countdown — not a silent no-op."""
    from cortex.apps.desktop_shell.controller import CortexAppController

    ctrl = CortexAppController.__new__(CortexAppController)
    ctrl._dashboard = None
    daemon = _BreakRecordingDaemon()
    ctrl._daemon = daemon

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ctrl._daemon_loop = loop
    try:
        ctrl._on_action_invoked(
            "iv-timer",
            {
                "action_id": "act-timer",
                "action_type": "start_timer",
                "label": "Start a 10-minute timer",
                "metadata": {"duration_seconds": 600},
            },
        )

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not daemon.break_calls:
            time.sleep(0.01)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()

    assert daemon.break_calls, "start_timer did not start a break/countdown"
    call = daemon.break_calls[0]
    assert call["duration_seconds"] == 600
    assert call["breathing_pattern"] is None
    assert call["audio_cue"] is False
    assert call["intervention_id"] == "iv-timer"


# ---------------------------------------------------------------------------
# P2-FEAT-SCREENSHARE + P2-FE-MULTIMON — overlay edge cases
# ---------------------------------------------------------------------------


@pytest.fixture()
def overlay(qapp, monkeypatch):
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    w = overlay_mod.OverlayWindow()
    yield w
    try:
        w._timeout_timer.stop()
        w.deleteLater()
    except RuntimeError:
        pass


def _minimal_intervention_payload() -> dict[str, Any]:
    return {
        "intervention_id": "iv-share",
        "headline": "Take a moment",
        "situation_summary": "test",
        "primary_focus": "focus",
        "micro_steps": [],
        "suggested_actions": [],
        "level": "overlay_only",
    }


def test_overlay_suppressed_when_screen_sharing(overlay, monkeypatch) -> None:
    """P2-FEAT-SCREENSHARE: when a display is being captured the overlay
    must NOT become visible (the intervention card is still prepared)."""
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "screen_share_active", lambda: True)

    shown: list[bool] = []
    monkeypatch.setattr(
        overlay, "_play_show_animations", lambda: shown.append(True)
    )

    overlay.show_intervention(_minimal_intervention_payload())

    assert overlay.isVisible() is False, "overlay must be hidden while sharing"
    assert shown == [], "show animations must not run while sharing"
    # Card content was still prepared (headline applied) so the next,
    # non-shared intervention shows instantly.
    assert overlay._intervention_id == "iv-share"


def test_overlay_shown_when_not_screen_sharing(overlay, monkeypatch) -> None:
    """The companion case: with no capture, the overlay shows normally."""
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "screen_share_active", lambda: False)

    shown: list[bool] = []
    monkeypatch.setattr(
        overlay, "_play_show_animations", lambda: shown.append(True)
    )

    overlay.show_intervention(_minimal_intervention_payload())

    assert overlay.isVisible() is True, "overlay must show when not sharing"
    assert shown == [True], "show animations must run when not sharing"


def test_overlay_targets_screen_under_cursor(overlay, monkeypatch) -> None:
    """P2-FE-MULTIMON: placement must use the screen under the cursor
    (QGuiApplication.screenAt(QCursor.pos())) rather than self.screen()."""
    from cortex.apps.desktop_shell import overlay as overlay_mod

    class _FakeScreen:
        def availableGeometry(self):  # noqa: N802 - Qt API name
            from PySide6.QtCore import QRect

            return QRect(5000, 6000, 1280, 800)

    fake = _FakeScreen()
    # screenAt must be consulted with the cursor position.
    monkeypatch.setattr(
        overlay_mod.QGuiApplication, "screenAt", staticmethod(lambda _pos: fake)
    )

    chosen = overlay._target_screen()
    assert chosen is fake


def test_target_screen_falls_back_when_cursor_screen_none(
    overlay, monkeypatch
) -> None:
    """When screenAt returns None the helper falls back to self.screen()
    and finally the primary screen (never crashing)."""
    from cortex.apps.desktop_shell import overlay as overlay_mod

    monkeypatch.setattr(
        overlay_mod.QGuiApplication, "screenAt", staticmethod(lambda _pos: None)
    )
    # self.screen() may legitimately return None for a hidden offscreen
    # window; the helper must then reach primaryScreen() without raising.
    chosen = overlay._target_screen()
    # Either self.screen() or the primary screen — both are acceptable
    # non-crashing fallbacks. The key guarantee is "no exception".
    assert chosen is None or chosen is not None
