"""RAISE_DASHBOARD reaches the desktop shell in both of its modes.

The daemon broadcast it to the desktop (a tap on a macOS banner, the
browser's "Open Cortex" through ``POST /dashboard/raise``) and neither
desktop routing table had an entry, so the request was dropped.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QApplication

from cortex.libs.schemas.ws_message_types import MessageType


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_websocket_mode_bridge_emits_a_raise_request(app: QApplication) -> None:
    from cortex.apps.desktop_shell.main import WebSocketBridge

    bridge = WebSocketBridge()
    raised: list[bool] = []
    bridge.raise_dashboard_requested.connect(lambda: raised.append(True))

    frame = json.dumps(
        {
            "type": MessageType.RAISE_DASHBOARD.value,
            "payload": {"target": None},
            "sequence": 7,
            "target_client_types": ["desktop"],
        }
    )
    assert bridge._message_router.dispatch_json(frame) is True
    app.processEvents()
    assert raised == [True]

    # A frame addressed to another client type is not the desktop's to act on.
    other = json.dumps(
        {
            "type": MessageType.RAISE_DASHBOARD.value,
            "payload": {"target": None},
            "sequence": 8,
            "target_client_types": ["chrome"],
        }
    )
    bridge._message_router.dispatch_json(other)
    app.processEvents()
    assert raised == [True]


def test_in_process_bridge_emits_a_raise_request(app: QApplication) -> None:
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    raised: list[bool] = []
    bridge.raise_dashboard_requested.connect(lambda: raised.append(True))
    bridge.on_raise_dashboard({"target": None})
    app.processEvents()
    assert raised == [True]


def test_in_process_observer_routes_the_daemon_broadcast(app: QApplication) -> None:
    """Drive the real observer the controller installs on the daemon's
    transport, with the event the daemon publishes for a banner tap."""

    from typing import Any

    from cortex.application.events import OutboundTransportEvent
    from cortex.apps.desktop_shell.controller import CortexAppController, DaemonBridge

    class _Subscription:
        def cancel(self) -> None:
            return None

    class _Transport:
        listener: Any = None

        def subscribe_outbound(self, listener: Any) -> _Subscription:
            self.listener = listener
            return _Subscription()

    class _Daemon:
        _ws_server = _Transport()

    controller = CortexAppController.__new__(CortexAppController)
    bridge = DaemonBridge()
    controller._bridge = bridge
    controller._daemon = _Daemon()
    controller._outbound_subscription = None
    controller._install_ws_broadcast_observer()

    raised: list[bool] = []
    bridge.raise_dashboard_requested.connect(lambda: raised.append(True))
    listener = controller._daemon._ws_server.listener
    assert listener is not None

    listener(
        OutboundTransportEvent(
            message_type=MessageType.RAISE_DASHBOARD.value,
            payload={"target": None},
            target_client_types=("desktop",),
        )
    )
    app.processEvents()
    assert raised == [True]

    # Addressed to a browser only: the desktop leaves it alone.
    listener(
        OutboundTransportEvent(
            message_type=MessageType.RAISE_DASHBOARD.value,
            payload={"target": None},
            target_client_types=("chrome",),
        )
    )
    app.processEvents()
    assert raised == [True]
