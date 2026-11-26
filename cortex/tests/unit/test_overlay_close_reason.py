"""Only a user's dismissal of the suggestion card is reported as a rejection.

Every way the card closes — ×, Escape or Cmd+D, the five-minute untouched
timeout, and the close that follows a 👍 or a 👎 comment — went through
one ``dismissed`` signal, and both hosts answered it with USER_ACTION
``"dismissed"``. The daemon's ``dismissed`` branch records a dismissal
(which can start the dismissal-driven quiet window), trains the dismissal
model with ``dismissed=True`` and records a consent rejection, so a
thumbs-up was booked as a rejection and so was a card nobody saw. A
timeout or a rated close is now reported as ``"expired"``, the neutral
close the browser panel already sends for its own timeout. The rated
close also no longer closes a newer card that appeared within its 180 ms.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Coroutine
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _arm(widget: Any, intervention_id: str) -> None:
    widget._intervention_id = intervention_id
    widget._dismissed = False
    widget._timeout_timer.start()


@pytest.fixture()
def overlay(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    widget = overlay_mod.OverlayWindow()
    _arm(widget, "iv-1")
    yield widget
    try:
        widget._timeout_timer.stop()
        widget.deleteLater()
    except RuntimeError:
        pass


def _pump(qapp: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


class _Daemon:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    async def _handle_user_action(self, payload: dict[str, Any]) -> None:
        self.actions.append(dict(payload))


def _run_now(coro: Coroutine[Any, Any, None], _loop: object) -> None:
    runner = asyncio.new_event_loop()
    try:
        runner.run_until_complete(coro)
    finally:
        runner.close()


@pytest.fixture()
def controller(overlay: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cortex.apps.desktop_shell import controller as controller_mod

    ctrl = controller_mod.CortexAppController.__new__(controller_mod.CortexAppController)
    ctrl._overlay = overlay
    ctrl._daemon = _Daemon()
    ctrl._daemon_loop = object()  # type: ignore[assignment]
    monkeypatch.setattr(controller_mod.asyncio, "run_coroutine_threadsafe", _run_now)
    overlay.dismissed.connect(ctrl._on_overlay_dismissed)
    return ctrl


def _terminal_actions(ctrl: Any) -> list[str]:
    return [a["action"] for a in ctrl._daemon.actions if "action" in a]


def test_user_dismiss_is_a_dismissal(controller: Any, overlay: Any) -> None:
    overlay._user_dismiss()
    assert _terminal_actions(controller) == ["dismissed"]


def test_untouched_timeout_is_not_a_rejection(controller: Any, overlay: Any) -> None:
    overlay._auto_dismiss()
    assert _terminal_actions(controller) == ["expired"]


def test_thumbs_up_closes_without_a_rejection(
    qapp: QApplication, controller: Any, overlay: Any,
) -> None:
    overlay._on_thumbs_up()
    _pump(qapp, 0.35)
    assert overlay._dismissed is True
    assert _terminal_actions(controller) == ["expired"]


def test_thumbs_down_comment_closes_without_a_rejection(
    qapp: QApplication, controller: Any, overlay: Any,
) -> None:
    overlay._on_thumbs_down()
    overlay._feedback_text.setText("not now")
    overlay._commit_feedback_text()
    _pump(qapp, 0.3)
    assert _terminal_actions(controller) == ["expired"]


def test_rated_close_leaves_a_newer_card_open(
    qapp: QApplication, controller: Any, overlay: Any,
) -> None:
    overlay._on_thumbs_up()
    # A new suggestion is shown before the 180 ms close fires.
    _arm(overlay, "iv-2")
    _pump(qapp, 0.35)
    assert overlay._dismissed is False
    assert overlay._intervention_id == "iv-2"
    assert _terminal_actions(controller) == []


def test_websocket_host_maps_close_reasons(
    qapp: QApplication, overlay: Any,
) -> None:
    from cortex.apps.desktop_shell.main import CortexApp

    class _Bridge:
        def __init__(self) -> None:
            self.actions: list[tuple[str, str]] = []

        def send_user_action(self, action: str, intervention_id: str) -> None:
            self.actions.append((action, intervention_id))

        def send_user_rating(self, *_args: Any) -> None:
            pass

    app = CortexApp()
    app._overlay = overlay
    app._bridge = _Bridge()  # type: ignore[assignment]
    app._wire_overlay()

    overlay._auto_dismiss()
    _arm(overlay, "iv-2")
    overlay._user_dismiss()

    assert app._bridge.actions == [  # type: ignore[union-attr]
        ("expired", "iv-1"),
        ("dismissed", "iv-2"),
    ]
