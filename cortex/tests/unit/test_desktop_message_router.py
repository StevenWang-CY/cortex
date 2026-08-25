from __future__ import annotations

import json

from cortex.apps.desktop_shell.message_router import DesktopMessageRouter


def test_router_normalizes_payload_and_drops_replays() -> None:
    observed: list[dict[str, object]] = []
    router = DesktopMessageRouter({"STATE_UPDATE": observed.append})

    assert router.dispatch_json(json.dumps({
        "type": "STATE_UPDATE",
        "payload": {"state": "FLOW"},
        "sequence": 2,
    })) is True
    assert router.dispatch("STATE_UPDATE", {"state": "HYPER"}, sequence=1) is False
    assert observed == [{"state": "FLOW"}]

    router.reset()
    assert router.dispatch("STATE_UPDATE", "malformed", sequence=1) is True
    assert observed[-1] == {}


def test_router_honors_target_surface_and_unknown_messages() -> None:
    observed: list[dict[str, object]] = []
    router = DesktopMessageRouter({"SESSION_RECAP": observed.append})

    assert router.dispatch(
        "SESSION_RECAP",
        {},
        target_client_types=["chrome"],
    ) is False
    assert router.dispatch(
        "SESSION_RECAP",
        {},
        target_client_types=["desktop"],
    ) is True
    assert router.dispatch("FUTURE_MESSAGE", {}) is False
    assert observed == [{}]
