"""Phase J-3 — Dashboard empty states.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_empty_state.py``

Before the first capture frame arrives, both the consumer biometrics
card and the advanced developer tab show "Start a session to ..."
placeholders so the user doesn't read the placeholder ``--`` glyphs as
"the daemon is broken". The first ``update_state`` call hides the
placeholders and the flag is sticky — a transient WS disconnect should
not collapse the UI back to the empty state.

The audit-w2 reconciliation noted the timeline panel already had a "No
events yet" empty state but the BPM card and the developer debug
widgets did not. Phase J-3 closes that gap.
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
def dashboard(qapp, monkeypatch):
    from cortex.apps.desktop_shell import dashboard as dashboard_mod
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    w = dashboard_mod.DashboardWindow()
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


def test_consumer_empty_state_visible_before_first_frame(dashboard):
    """The biometrics card carries the 'Start a session' placeholder
    paragraph before any state arrives."""
    consumer = dashboard._consumer
    assert consumer._has_received_state is False
    placeholder = consumer._bio_empty_state
    assert placeholder is not None
    assert "Start a session" in placeholder.text()
    assert "biometrics" in placeholder.text().lower()
    # The widget is constructed visible-by-default; under offscreen it
    # only becomes effectively-on-screen when the dashboard is shown,
    # but the contract is that setVisible(False) has NOT been called yet.
    assert consumer._bio_empty_state.isVisibleTo(consumer)


def test_consumer_empty_state_hidden_after_first_update(dashboard):
    """The first ``update_state`` call retires the placeholder."""
    consumer = dashboard._consumer
    consumer.update_state({"state": "FLOW", "biometrics": {"heart_rate": 72.0}})
    assert consumer._has_received_state is True
    assert not consumer._bio_empty_state.isVisibleTo(consumer)


def test_consumer_empty_state_stays_hidden_after_reconnect(dashboard):
    """The flag is sticky: cached biometrics survive a reconnect, so
    the empty-state should never come back once we've seen a frame."""
    consumer = dashboard._consumer
    consumer.update_state({"state": "FLOW", "biometrics": {"heart_rate": 72.0}})
    # Simulate a disconnect callback.
    consumer.set_connected(False)
    assert not consumer._bio_empty_state.isVisibleTo(consumer)
    # Re-connect with no state change.
    consumer.set_connected(True)
    assert not consumer._bio_empty_state.isVisibleTo(consumer)
    # Second update — still hidden.
    consumer.update_state({"state": "RECOVERY", "biometrics": {"heart_rate": 65.0}})
    assert not consumer._bio_empty_state.isVisibleTo(consumer)


def test_advanced_empty_state_visible_before_first_frame(dashboard):
    """The developer-debug tab also gets an empty-state panel."""
    advanced = dashboard._advanced
    assert advanced._has_received_state is False
    placeholder = advanced._empty_state
    assert placeholder is not None
    assert "Start a session" in placeholder.text()
    # Mentions at least one of the dev-debug widgets so the user knows
    # what they're about to populate.
    text = placeholder.text().lower()
    assert any(kw in text for kw in ("signal quality", "heart-rate", "state scores"))
    assert advanced._empty_state.isVisibleTo(advanced)


def test_advanced_empty_state_hidden_after_first_update(dashboard):
    """First ``update_state`` retires the placeholder on the advanced tab."""
    advanced = dashboard._advanced
    advanced.update_state(
        {
            "state": "FLOW",
            "biometrics": {"heart_rate": 72.0},
            "scores": {"flow": 0.8},
            "signal_quality": {"physio": 0.9},
            "confidence": 0.85,
            "dwell_seconds": 12.5,
        }
    )
    assert advanced._has_received_state is True
    assert not advanced._empty_state.isVisibleTo(advanced)


def test_dashboard_update_state_propagates_to_both_tabs(dashboard):
    """``DashboardWindow.update_state`` forwards to both _ConsumerTab and
    _AdvancedTab; the empty-state on both must clear from a single call."""
    dashboard.update_state(
        {
            "state": "HYPER",
            "biometrics": {"heart_rate": 95.0},
            "scores": {"hyper": 0.9},
            "signal_quality": {"physio": 0.7},
            "confidence": 0.92,
            "dwell_seconds": 4.2,
        }
    )
    assert dashboard._consumer._has_received_state is True
    assert dashboard._advanced._has_received_state is True
    assert not dashboard._consumer._bio_empty_state.isVisibleTo(dashboard._consumer)
    assert not dashboard._advanced._empty_state.isVisibleTo(dashboard._advanced)


def test_empty_state_has_accessible_name(dashboard):
    """Phase J-5 (a11y sweep) preview: empty-state placeholders need
    accessible names so VoiceOver doesn't announce them as a generic
    'text' element."""
    assert dashboard._consumer._bio_empty_state.accessibleName()
    assert dashboard._advanced._empty_state.accessibleName()
