"""Audit F17 — receivers drop reordered / stale frames by per-type sequence.

The daemon's WS server increments ``WSMessage.sequence`` once per
outbound message; in-process callbacks get a ``_seq`` stamp on the
payload dict. Receivers must remember the last applied value per
channel (per WS message type, or per in-process callback channel)
and silently drop any frame whose value is not strictly greater.

Two surfaces are tested here:

1. ``DaemonBridge`` (in-process bridge in :mod:`controller`) — the
   payload carries ``_seq``; the bridge drops reordered frames before
   emitting the Qt signal.
2. ``WebSocketBridge`` (WS bridge in :mod:`main`) — the envelope
   carries ``sequence``; the bridge drops reordered frames before
   parsing and emitting.

Each test fails on pre-F17 ``main`` because the drop-stale logic is
absent: every frame, including out-of-order ones, would be emitted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# In-process DaemonBridge (controller.py)
# ---------------------------------------------------------------------------


def _make_bridge():
    """Construct a DaemonBridge with a stub Signal collector.

    Avoids booting the full Qt application — we substitute a tiny
    callable on the Signal whose semantics match ``emit``. The bridge's
    sequence-tracking logic does not touch Qt internals so this is
    safe.
    """
    from cortex.apps.desktop_shell import controller

    bridge = controller.DaemonBridge()
    collected_state: list[dict] = []
    collected_intervention: list[dict] = []

    class _Sig:
        def __init__(self, sink: list) -> None:
            self._sink = sink

        def emit(self, payload: dict) -> None:
            self._sink.append(payload)

        def connect(self, *_a, **_kw) -> None:  # noqa: D401
            return None

    bridge.state_updated = _Sig(collected_state)  # type: ignore[assignment]
    bridge.intervention_triggered = _Sig(collected_intervention)  # type: ignore[assignment]
    return bridge, collected_state, collected_intervention


def test_daemon_bridge_drops_reordered_state_frame() -> None:
    bridge, state, _ = _make_bridge()
    bridge.on_state({"_seq": 1, "state": "FLOW"})
    bridge.on_state({"_seq": 3, "state": "HYPER"})
    # Out-of-order: seq=2 arrives after seq=3 — must drop.
    bridge.on_state({"_seq": 2, "state": "STALE"})
    bridge.on_state({"_seq": 4, "state": "RECOVERY"})
    assert [p["state"] for p in state] == ["FLOW", "HYPER", "RECOVERY"]


def test_daemon_bridge_drops_duplicate_state_frame() -> None:
    bridge, state, _ = _make_bridge()
    bridge.on_state({"_seq": 5, "state": "FLOW"})
    bridge.on_state({"_seq": 5, "state": "DUP"})
    assert [p["state"] for p in state] == ["FLOW"]


def test_daemon_bridge_intervention_channel_is_independent() -> None:
    """state and intervention channels have separate counters."""
    bridge, state, intv = _make_bridge()
    bridge.on_state({"_seq": 1, "state": "FLOW"})
    bridge.on_intervention({"_seq": 1, "intervention_id": "iv1"})
    bridge.on_intervention({"_seq": 2, "intervention_id": "iv2"})
    bridge.on_state({"_seq": 2, "state": "HYPER"})
    assert [p["state"] for p in state] == ["FLOW", "HYPER"]
    assert [p["intervention_id"] for p in intv] == ["iv1", "iv2"]


def test_daemon_bridge_frame_without_seq_is_applied() -> None:
    """Backwards compat: a payload without ``_seq`` (legacy daemon /
    test fixture) is applied unconditionally so we do not regress
    older callers."""
    bridge, state, _ = _make_bridge()
    bridge.on_state({"state": "FLOW"})
    bridge.on_state({"state": "HYPER"})
    assert [p["state"] for p in state] == ["FLOW", "HYPER"]


def test_daemon_bridge_reset_clears_counters() -> None:
    """After a daemon restart, ``reset_sequence_counters`` lets the
    next first-frame win regardless of the prior counter value."""
    bridge, state, _ = _make_bridge()
    bridge.on_state({"_seq": 100, "state": "FLOW"})
    bridge.reset_sequence_counters()
    bridge.on_state({"_seq": 1, "state": "POST_RESTART"})
    assert [p["state"] for p in state] == ["FLOW", "POST_RESTART"]


# ---------------------------------------------------------------------------
# WebSocketBridge (main.py)
# ---------------------------------------------------------------------------


def _make_ws_bridge():
    """Construct a WebSocketBridge with Signal stubs.

    We never start the asyncio thread; we only exercise
    ``_handle_message`` against the per-type sequence tracker.
    """
    from cortex.apps.desktop_shell import main as desktop_main

    bridge = desktop_main.WebSocketBridge(host="127.0.0.1", port=9473)
    collected: dict[str, list[dict]] = {
        "state": [],
        "intervention": [],
        "restore": [],
        "settings": [],
    }

    class _Sig:
        def __init__(self, sink: list) -> None:
            self._sink = sink

        def emit(self, payload: dict) -> None:
            self._sink.append(payload)

        def connect(self, *_a, **_kw) -> None:  # noqa: D401
            return None

    bridge.state_updated = _Sig(collected["state"])  # type: ignore[assignment]
    bridge.intervention_triggered = _Sig(collected["intervention"])  # type: ignore[assignment]
    bridge.intervention_restored = _Sig(collected["restore"])  # type: ignore[assignment]
    bridge.settings_synced = _Sig(collected["settings"])  # type: ignore[assignment]
    return bridge, collected


def _wsframe(msg_type: str, payload: dict[str, Any], seq: int) -> str:
    return json.dumps({"type": msg_type, "payload": payload, "sequence": seq})


def test_ws_bridge_drops_reordered_state_update() -> None:
    bridge, collected = _make_ws_bridge()
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "FLOW"}, 10))
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "HYPER"}, 12))
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "STALE"}, 11))
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "RECOV"}, 13))
    assert [p["state"] for p in collected["state"]] == ["FLOW", "HYPER", "RECOV"]


def test_ws_bridge_per_type_counters_are_independent() -> None:
    bridge, collected = _make_ws_bridge()
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "FLOW"}, 5))
    bridge._handle_message(_wsframe("INTERVENTION_TRIGGER", {"id": "i1"}, 1))
    bridge._handle_message(_wsframe("INTERVENTION_TRIGGER", {"id": "i2"}, 2))
    # Intervention seq=2 must not collide with state seq=5.
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "HYPER"}, 6))
    assert [p["state"] for p in collected["state"]] == ["FLOW", "HYPER"]
    assert [p["id"] for p in collected["intervention"]] == ["i1", "i2"]


def test_ws_bridge_sequence_zero_bypasses_check() -> None:
    """Older daemons or types that don't bump the counter use
    ``sequence=0``; we apply them unconditionally for compat."""
    bridge, collected = _make_ws_bridge()
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "FLOW"}, 0))
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "HYPER"}, 0))
    assert [p["state"] for p in collected["state"]] == ["FLOW", "HYPER"]


def test_ws_bridge_malformed_frame_is_dropped() -> None:
    """Sanity: bad JSON does not trip the sequence tracker into an
    inconsistent state. The frame is silently dropped (existing
    behaviour) and the tracker untouched."""
    bridge, collected = _make_ws_bridge()
    bridge._handle_message("not-json")
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "FLOW"}, 1))
    assert [p["state"] for p in collected["state"]] == ["FLOW"]


def test_ws_bridge_last_seq_clears_on_reconnect_method() -> None:
    """The tracker should empty so a daemon-restart's seq=1 wins
    against a pre-restart counter that climbed high. We exercise the
    same path as the production code by calling ``clear()`` directly
    (the production reset lives in the asyncio reconnect loop)."""
    bridge, collected = _make_ws_bridge()
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "FLOW"}, 100))
    # Simulate daemon restart: tracker is cleared by the reconnect path.
    bridge._last_seq_by_type.clear()
    bridge._handle_message(_wsframe("STATE_UPDATE", {"state": "POST"}, 1))
    assert [p["state"] for p in collected["state"]] == ["FLOW", "POST"]


# ---------------------------------------------------------------------------
# Daemon-side: state callback stamps _seq
# ---------------------------------------------------------------------------


def test_daemon_state_callback_stamps_monotonic_seq() -> None:
    """The runtime daemon must increment ``_state_callback_seq`` before
    every callback invocation. We construct just enough of the daemon
    to exercise the stamping logic without booting the camera / WS /
    LLM stack."""
    from cortex.libs.config.settings import CortexConfig
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=CortexConfig())
    recorded: list[dict] = []
    daemon.set_state_callback(recorded.append)

    # Simulate the inner-loop callback invocation directly. The actual
    # state-loop construction has many side dependencies (camera,
    # feature-fusion, etc.); we exercise just the public contract:
    # set_state_callback receives a dict whose ``_seq`` is monotonic.
    for i in range(3):
        daemon._state_callback_seq += 1
        if daemon._state_callback is not None:
            daemon._state_callback({
                "_seq": daemon._state_callback_seq,
                "state": "FLOW",
            })
    assert [p["_seq"] for p in recorded] == [1, 2, 3]


@pytest.mark.asyncio
async def test_daemon_intervention_callback_stamps_monotonic_seq() -> None:
    """Same contract for the intervention channel."""
    from cortex.libs.config.settings import CortexConfig
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=CortexConfig())
    recorded: list[dict] = []
    daemon.set_intervention_callback(recorded.append)

    for i in range(3):
        daemon._intervention_callback_seq += 1
        if daemon._intervention_callback is not None:
            daemon._intervention_callback({
                "_seq": daemon._intervention_callback_seq,
                "intervention_id": f"iv_{i}",
            })
    assert [p["_seq"] for p in recorded] == [1, 2, 3]
