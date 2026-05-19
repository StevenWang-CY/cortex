"""Audit F55 — accessibility names, tab-order chain, contrast ratios.

Pre-fix:

* Several interactive widgets in Dashboard + Overlay lacked
  ``setAccessibleName`` so VoiceOver / screen readers announced raw
  ObjectClass names ("QPushButton", "QLineEdit") instead of the human
  label.
* No widget had ``setTabOrder`` wired explicitly; the tab chain
  depended on construction order, which a single re-arrangement could
  silently scramble.
* ``_LABEL_TERTIARY = "#827971"`` against ``#FFFFFF`` computed to
  ~3.98:1 — just below WCAG AA's 4.5:1 threshold for normal-weight
  text. The role is "tertiary captions / placeholders" so the volume
  affected is high (every QLineEdit placeholder, every "Confidence:
  --" debug label).

Three test cases below:

1. Every interactive widget in _ConsumerTab and OverlayWindow has a
   non-empty ``accessibleName()``.
2. The tab-order chain is established (next-in-chain of one widget
   resolves to the next expected widget).
3. ``_LABEL_TERTIARY`` against ``_CONTROL_BG`` meets WCAG AA via a
   hand-rolled contrast-ratio computation.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _pyside6_is_mocked() -> bool:
    """``test_desktop_shell.py`` installs lightweight mock PySide6
    modules in ``sys.modules`` at import time. The mock module has no
    ``__file__`` attribute; the real C extension always does. If we
    detect the mock and try to delete it + re-import the real PySide6,
    the C state from the first real-PySide6 load (which test_desktop_shell
    inherited before swapping) double-loads and segfaults.

    Cleanest workaround: skip the Qt-touching tests when the mock is
    detected. The same tests run cleanly in isolation, and the wider
    audit budget still requires this finding's failing-on-main contract
    to hold against the unmodified branch — which it does."""
    pyside6 = sys.modules.get("PySide6")
    if pyside6 is None:
        return False
    return getattr(pyside6, "__file__", None) is None


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_if_pyside6_mocked():
    """Per-test guard against the session-state pollution that
    test_desktop_shell.py introduces. Pytest collection imports every
    test module up-front; test_desktop_shell installs mock PySide6
    modules at import time and never restores the real ones, so a Qt
    test that runs AFTER test_desktop_shell is collected will see the
    mocks even if it was IMPORTED earlier alphabetically. We re-check
    at fixture time (when the test actually runs) and skip if needed."""
    if _pyside6_is_mocked():
        pytest.skip(
            "PySide6 mocked by an earlier test in this session; "
            "run this file in isolation",
        )


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


# --- contrast helpers --------------------------------------------------------


def _hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    hex_code = hex_code.lstrip("#")
    if len(hex_code) == 3:
        hex_code = "".join(c * 2 for c in hex_code)
    return tuple(int(hex_code[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (_channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 relative luminance contrast ratio between two hex colors.

    Reference: https://www.w3.org/TR/WCAG21/#contrast-minimum
    The ratio is symmetric in (fg, bg) — the formula divides the
    lighter by the darker channel, both offset by 0.05.
    """
    lum_fg = _relative_luminance(_hex_to_rgb(fg))
    lum_bg = _relative_luminance(_hex_to_rgb(bg))
    lighter = max(lum_fg, lum_bg)
    darker = min(lum_fg, lum_bg)
    return (lighter + 0.05) / (darker + 0.05)


# --- tests -------------------------------------------------------------------


def test_label_tertiary_meets_wcag_aa_against_control_bg() -> None:
    """``_LABEL_TERTIARY`` is used for placeholders and captions on the
    dashboard's ``_CONTROL_BG`` (#FFFFFF). WCAG AA for normal-weight
    text requires >= 4.5:1."""
    from cortex.apps.desktop_shell import dashboard

    ratio = _contrast_ratio(dashboard._LABEL_TERTIARY, dashboard._CONTROL_BG)
    assert ratio >= 4.5, (
        f"_LABEL_TERTIARY {dashboard._LABEL_TERTIARY} on "
        f"{dashboard._CONTROL_BG} is {ratio:.2f}:1 — fails WCAG AA "
        "(>= 4.5:1)"
    )


def test_consumer_tab_interactive_widgets_have_accessible_names(
    qapp: QApplication,
) -> None:
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    expected = {
        "_goal_input": "Goal",
        "_connect_btn": "Open Connections panel",
        "_stop_btn": "Stop Cortex",
    }
    for attr, name in expected.items():
        widget = getattr(consumer, attr)
        actual = widget.accessibleName()
        assert actual == name, (
            f"{attr}.accessibleName() = {actual!r}, expected {name!r}"
        )


def test_consumer_tab_tab_order_chain(qapp: QApplication) -> None:
    """The Goal → Connect → Stop chain must be established via
    ``setTabOrder``. We probe Qt's focus-next-prev linkage."""
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    # Walk the focus chain from the goal input. setTabOrder establishes
    # nextInFocusChain() so we can verify the chain end-to-end.
    visited = []
    node = consumer._goal_input
    for _ in range(50):  # bounded walk in case Qt cycles
        visited.append(node)
        node = node.nextInFocusChain()
        if node in (consumer._stop_btn, None):
            break
    if node is consumer._stop_btn:
        visited.append(node)

    # The chain must contain the three interactive widgets in order.
    indices = {
        id(consumer._goal_input): None,
        id(consumer._connect_btn): None,
        id(consumer._stop_btn): None,
    }
    for i, w in enumerate(visited):
        if id(w) in indices and indices[id(w)] is None:
            indices[id(w)] = i

    assert all(v is not None for v in indices.values()), (
        f"focus chain did not visit all three widgets; visited {indices}"
    )
    assert (
        indices[id(consumer._goal_input)]
        < indices[id(consumer._connect_btn)]
        < indices[id(consumer._stop_btn)]
    ), f"tab order broken: {indices}"


def test_overlay_dismiss_and_toggle_have_accessible_names(
    qapp: QApplication,
) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    assert win._dismiss_btn.accessibleName() == "Dismiss intervention"
    assert win._causal_toggle.accessibleName() == (
        "Show full causal explanation"
    )


def test_overlay_hud_palette_meets_high_alpha_contract() -> None:
    """The HUD primary text token must keep its high-alpha contract so
    contrast against the dark vibrancy material exceeds 7:1 (effectively
    AAA for white text on near-black)."""
    from cortex.apps.desktop_shell.tokens import TEXT_HUD_PRIMARY

    # alpha >= 0.9 ensures the rendered foreground stays close to pure
    # white when composited over the HUD material.
    assert TEXT_HUD_PRIMARY[3] >= 230, (
        f"TEXT_HUD_PRIMARY alpha {TEXT_HUD_PRIMARY[3]} too low — would "
        "wash out against the dark vibrancy material"
    )
