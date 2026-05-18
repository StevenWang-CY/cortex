"""Audit Wave 2 — every top-level window applies macOS native chrome.

F47 + commit 2030f3d enabled ``apply_unified_titlebar`` +
``apply_vibrancy`` on the dashboard, settings, onboarding, overlay, and
connections panels. A regression here would be silent — a new window
class that forgets the call picks up the default Qt chrome (visible
title text, no transparent titlebar, no system vibrancy material). The
test enumerates the top-level window classes and asserts each one
emits both calls inside its ``showEvent`` body.

Test contract:

1. Every ``class FooWindow(QWidget)`` / ``QDialog`` declared in
   ``cortex/apps/desktop_shell/`` and treated as a top-level surface
   defines a ``showEvent`` method.
2. The ``showEvent`` body calls ``mac_native.apply_unified_titlebar``.
3. The ``showEvent`` body calls ``mac_native.apply_vibrancy``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SHELL_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "desktop_shell"
)

# (file, class_name) pairs for surfaces that must apply native chrome.
TOP_LEVEL_WINDOWS: tuple[tuple[str, str], ...] = (
    ("dashboard.py", "DashboardWindow"),
    ("settings.py", "SettingsDialog"),
    ("onboarding.py", "OnboardingWindow"),
    ("overlay.py", "OverlayWindow"),
    ("connections.py", "ConnectionsPanel"),
)


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _find_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _method_calls_attribute(method: ast.FunctionDef, attr: str) -> bool:
    """True when ``method`` invokes any callable whose attribute name
    matches ``attr`` (handles ``mac_native.apply_xxx(...)`` regardless
    of how mac_native was bound — module attribute, alias, etc.)."""
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attr:
            return True
    return False


@pytest.mark.parametrize(("filename", "class_name"), TOP_LEVEL_WINDOWS)
def test_window_applies_unified_titlebar(
    filename: str, class_name: str
) -> None:
    source = (SHELL_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = _find_class(tree, class_name)
    assert cls is not None, f"class {class_name} not found in {filename}"
    show_event = _find_method(cls, "showEvent")
    assert show_event is not None, (
        f"{class_name} must define showEvent to apply native chrome"
    )
    assert _method_calls_attribute(show_event, "apply_unified_titlebar"), (
        f"{class_name}.showEvent does not call apply_unified_titlebar — "
        "the window will keep Qt's default opaque titlebar."
    )


@pytest.mark.parametrize(("filename", "class_name"), TOP_LEVEL_WINDOWS)
def test_window_applies_vibrancy(filename: str, class_name: str) -> None:
    source = (SHELL_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = _find_class(tree, class_name)
    assert cls is not None, f"class {class_name} not found in {filename}"
    show_event = _find_method(cls, "showEvent")
    assert show_event is not None
    assert _method_calls_attribute(show_event, "apply_vibrancy"), (
        f"{class_name}.showEvent does not call apply_vibrancy — the "
        "window will not pick up the NSVisualEffect material."
    )
