"""P2-5 — HistoryTab session-list loading state.

The session-list pane (``_TodayPanel``) should show a "Loading…" label
while a ``SESSION_LIST`` response is in-flight and hide it once the
response arrives.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_history_tab_list_loading.py``
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


@pytest.fixture()
def history_tab(qapp):
    from cortex.apps.desktop_shell import history_tab as ht_mod

    tab = ht_mod.HistoryTab()
    yield tab
    try:
        tab.deleteLater()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Test 1: loading label hidden initially
# ---------------------------------------------------------------------------

def test_loading_label_hidden_initially(history_tab):
    panel = history_tab._today_panel
    assert panel._list_loading is False, "No loading in progress initially"
    assert panel._list_loading_label.isHidden(), (
        "Loading label must be hidden (isHidden()==True) before any request"
    )


# ---------------------------------------------------------------------------
# Test 2: loading label visible after history_requested signal
# ---------------------------------------------------------------------------

def test_loading_label_shows_when_list_requested(history_tab):
    """Simulating the _maybe_request_for_index call that shows the loading label."""
    # Prime the state — clear _requested so the next call triggers a fetch.
    history_tab._requested.clear()

    # Hook history_requested to prevent it from reaching the daemon.
    emitted: list = []
    history_tab.history_requested.connect(lambda *a: emitted.append(a))

    # Call the internal method directly to keep the test synchronous.
    history_tab._maybe_request_for_index(0)

    panel = history_tab._today_panel
    # Qt's isVisible() requires the whole widget hierarchy to be shown;
    # in headless tests the widget is not shown so we check the internal
    # flag and the label's own show/hide state via isHidden().
    assert panel._list_loading is True, (
        "_list_loading flag must be True after history_requested is emitted"
    )
    assert not panel._list_loading_label.isHidden(), (
        "Loading label must not be hidden (i.e. setVisible(True) was called)"
    )
    assert len(emitted) == 1, "history_requested must have been emitted once"


# ---------------------------------------------------------------------------
# Test 3: loading label hidden after apply_session_list
# ---------------------------------------------------------------------------

def test_loading_label_hides_after_response(history_tab):
    """Once SESSION_LIST lands the loading label must disappear."""
    panel = history_tab._today_panel

    # Force loading state on.
    panel.set_list_loading(True)
    assert panel._list_loading is True, "Precondition: must be loading"
    assert not panel._list_loading_label.isHidden(), (
        "Precondition: loading label must be shown"
    )

    # Deliver a minimal SESSION_LIST payload.
    history_tab.apply_session_list({
        "items": [],
        "total_known": 0,
        "next_cursor": None,
    })

    assert panel._list_loading is False, (
        "_list_loading flag must be cleared after apply_session_list"
    )
    assert panel._list_loading_label.isHidden(), (
        "Loading label must be hidden after apply_session_list"
    )


# ---------------------------------------------------------------------------
# Test 4: _list_loading flag reflects state correctly
# ---------------------------------------------------------------------------

def test_list_loading_flag(history_tab):
    panel = history_tab._today_panel

    panel.set_list_loading(True)
    assert panel._list_loading is True

    panel.set_list_loading(False)
    assert panel._list_loading is False
