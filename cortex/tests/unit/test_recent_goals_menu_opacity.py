"""Recent-goals menu must render an OPAQUE surface (UI bug fix).

The goal field's inline ⌄ affordance opened a ``QMenu`` with no background,
so on the vibrancy (translucent) window it painted see-through and its items
bled onto the Biometrics card beneath it. These tests pin the fix:

* the app-wide ``_GLOBAL_QSS`` defines an opaque ``QMenu`` rule, and
* the menu built by ``_open_recent_goals_menu`` carries an opaque,
  objectName-scoped stylesheet on the instance itself (defense against the
  cascade not reaching a top-level popup on macOS).

Run isolated/offscreen like the other real-Qt desktop tests.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("PySide6.QtWidgets")

import cortex.apps.desktop_shell.dashboard as dash  # noqa: E402


def test_global_qss_defines_opaque_menu() -> None:
    qss = dash._GLOBAL_QSS
    assert "QMenu {" in qss, "global stylesheet must style QMenu"
    # An opaque control-surface background (not transparent / unset).
    assert "background-color" in qss.split("QMenu {", 1)[1][:200]
    assert dash._CONTROL_BG in qss


def test_recent_goals_menu_builder_sets_opaque_scoped_style() -> None:
    """The menu builder forces an opaque, objectName-scoped surface.

    Asserted at the source level: constructing a real ``QMenu`` inside pytest
    crashes PySide6 (documented), so we verify the wiring statically. The
    live run exercises the actual rendering.
    """
    src = inspect.getsource(dash._ConsumerTab._open_recent_goals_menu)
    assert 'setObjectName("RecentGoalsMenu")' in src
    assert "setStyleSheet(" in src
    assert "background-color" in src
    assert "_CONTROL_BG" in src
    # Defensive opacity against the translucent parent window.
    assert "WA_TranslucentBackground" in src
