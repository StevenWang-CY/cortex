"""Shared accessibility helpers for the macOS desktop shell.

Extracted from ``dashboard.py`` + ``overlay.py`` (F55) so the connections
panel, settings dialog, onboarding wizard, and any future surface use the
same defensive wrappers. The wrappers no-op when the target widget is a
lightweight test stub without the AccessibleName/Description/TabOrder
methods (the legacy mock suite in ``test_desktop_shell.py`` is a frequent
caller).

VoiceOver-relevant policy:

* ``setAccessibleName`` is the short label VoiceOver announces.
* ``setAccessibleDescription`` is the longer hint, spoken after a pause.
* ``setFocusPolicy(Qt.StrongFocus)`` ensures the widget participates in
  keyboard tabbing — combined with ``setTabOrder``, this guarantees the
  focus ring is reachable without the mouse.

These helpers exist so a single ``import`` in each panel covers every
a11y affordance the audit (F55) requires.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def set_accessible_name(widget: Any, name: str) -> None:
    """Apply ``setAccessibleName`` if the widget supports it.

    Designed to be safe under the lightweight mock PySide6 stubs the
    legacy test suite installs — a missing method silently no-ops.
    """
    fn = getattr(widget, "setAccessibleName", None)
    if callable(fn):
        try:
            fn(name)
        except Exception:  # pragma: no cover - defensive
            logger.debug("setAccessibleName failed", exc_info=True)


def set_accessible_description(widget: Any, description: str) -> None:
    """Apply ``setAccessibleDescription`` if supported (else no-op)."""
    fn = getattr(widget, "setAccessibleDescription", None)
    if callable(fn):
        try:
            fn(description)
        except Exception:  # pragma: no cover
            logger.debug("setAccessibleDescription failed", exc_info=True)


def set_tab_order(first: Any, second: Any) -> None:
    """Apply ``QWidget.setTabOrder(first, second)`` if PySide6 exposes it.

    The lightweight mock stubs do not implement ``setTabOrder`` — under
    those harnesses this wrapper is a defensive no-op.
    """
    try:
        from PySide6.QtWidgets import QWidget
    except Exception:  # pragma: no cover - PySide6 missing
        return
    fn = getattr(QWidget, "setTabOrder", None)
    if callable(fn):
        try:
            fn(first, second)
        except Exception:  # pragma: no cover - mock widgets
            logger.debug("setTabOrder failed", exc_info=True)


def chain_tab_order(*widgets: Any) -> None:
    """Convenience wrapper: chains a sequence of widgets via
    :func:`set_tab_order` so the keyboard tab cycle walks them in order.

    Empty / single-element sequences are no-ops.
    """
    for first, second in zip(widgets, widgets[1:], strict=False):
        set_tab_order(first, second)


__all__ = [
    "chain_tab_order",
    "set_accessible_description",
    "set_accessible_name",
    "set_tab_order",
]
