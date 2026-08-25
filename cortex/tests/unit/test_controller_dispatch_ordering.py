"""Controller exact-action scheduling tests.

The in-process desktop shell is a user-gesture surface only. Every action is
submitted once to ``dispatch_intervention_action`` and the controller never
performs a native capability or emits an optimistic engaged/result record.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_controller_dispatch_ordering.py``
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Same PySide6-vs-stub guard the dashboard test uses.
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
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _RecordingDaemon:
    """Captures the order in which the controller calls daemon methods.

    ``dispatch_intervention_action`` is an async coroutine on the real daemon.
    """

    def __init__(self) -> None:
        self.workspace_mutation_allowed = True
        self.call_order: list[tuple[str, dict[str, Any]]] = []

    async def start_biology_break(self, **_kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def dispatch_intervention_action(
        self, intervention_id: str, action: dict
    ) -> int:
        # Sleep a beat to expose an ordering bug if one is reintroduced
        # (a buggy implementation that fires engage in parallel would
        # see engage land first while we're sleeping here).
        await asyncio.sleep(0.01)
        self.call_order.append(
            ("dispatch", {"intervention_id": intervention_id, "action": action})
        )
        return 1

    async def _handle_user_action(self, payload: dict) -> None:
        self.call_order.append(("user_action", payload))


@pytest.fixture()
def controller_with_loop(qapp):
    """Build a CortexAppController bypassing run(), wire a stand-in
    daemon, and spin up a real asyncio loop in a background thread so
    ``run_coroutine_threadsafe`` works exactly as in production.
    """
    from cortex.apps.desktop_shell.controller import CortexAppController

    ctrl = CortexAppController.__new__(CortexAppController)
    ctrl._dashboard = None
    ctrl._daemon = _RecordingDaemon()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ctrl._daemon_loop = loop

    try:
        yield ctrl
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()


def _valid_action() -> dict[str, Any]:
    return {
        "action_id": "act-1",
        "action_type": "close_tab",
        "label": "Close noisy tab",
        "reason": "test",
        "tab_index": 2,
    }


def test_exact_dispatch_is_the_only_effect_under_real_loop(
    controller_with_loop,
) -> None:
    """Even with a delayed daemon call, no optimistic side path runs."""
    ctrl = controller_with_loop
    ctrl._on_action_invoked("iv_active", _valid_action())

    # Wait for the daemon-side queue to drain.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(ctrl._daemon.call_order) >= 1:
            break
        time.sleep(0.01)

    kinds = [k for k, _ in ctrl._daemon.call_order]
    assert kinds == ["dispatch"]


def test_irreversible_suggestion_is_still_only_an_exact_request(
    controller_with_loop,
) -> None:
    ctrl = controller_with_loop
    ctrl._on_action_invoked(
        "iv_active",
        {
            "action_id": "act-2",
            "action_type": "start_timer",
            "label": "Start break",
            "reason": "test",
        },
    )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(ctrl._daemon.call_order) >= 1:
            break
        time.sleep(0.01)

    kinds = [k for k, _ in ctrl._daemon.call_order]
    assert kinds == ["dispatch"]


def test_no_daemon_loop_returns_silently(qapp) -> None:
    """When the daemon loop hasn't been started yet (e.g. early UI
    callbacks fired before ``run()``), the handler must no-op rather
    than crash — there is nowhere for the coroutine to land.
    """
    from cortex.apps.desktop_shell.controller import CortexAppController

    ctrl = CortexAppController.__new__(CortexAppController)
    ctrl._dashboard = None
    ctrl._daemon = _RecordingDaemon()
    ctrl._daemon_loop = None

    # Should not raise.
    ctrl._on_action_invoked("iv_active", _valid_action())
    assert ctrl._daemon.call_order == []
