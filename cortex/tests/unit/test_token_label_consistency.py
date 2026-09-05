"""Audit Wave 2 — every desktop-shell surface pulls warm-label tints from the
token registry, not private hex copies.

F55 raised the tertiary label tint in ``dashboard.py`` from "#827971" (3.98:1
against #FFFFFF — under WCAG AA's 4.5:1 threshold for normal-weight text) to
"#6B6661" (~5.4:1, AA-compliant). The connections panel, settings dialog, and
onboarding wizard kept the sub-AA literal. This test pins the token registry
as the source of truth so a future surface cannot regress to a hand-typed
"#827971" without breaking CI.

Test contract:

1. ``tokens.CX_TEXT_TERTIARY`` equals "#6B6661" (post-F55 value).
2. The four surfaces (dashboard, connections, settings, onboarding) do **not**
   contain the legacy "#827971" literal anywhere in their source.
3. The same four surfaces import ``CX_TEXT_TERTIARY`` / ``CX_TEXT_SECONDARY``
   from the registry directly and carry no private ``_LABEL_*`` alias
   block that could drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SHELL_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "desktop_shell"
)
SURFACE_FILES = (
    "dashboard.py",
    "connections.py",
    "settings.py",
    "onboarding.py",
)


def test_tokens_tertiary_is_wcag_aa_value() -> None:
    """The token registry pins the AA-passing tertiary tint."""
    from cortex.apps.desktop_shell.tokens import CX_TEXT_TERTIARY

    assert CX_TEXT_TERTIARY == "#6B6661"


@pytest.mark.parametrize("surface", SURFACE_FILES)
def test_surface_has_no_legacy_827971_literal(surface: str) -> None:
    """No surface may carry the sub-AA "#827971" tint literally."""
    source = (SHELL_DIR / surface).read_text(encoding="utf-8")
    assert "#827971" not in source, (
        f"{surface} still contains the legacy '#827971' tertiary tint — "
        f"replace with CX_TEXT_TERTIARY from tokens.py."
    )


@pytest.mark.parametrize(
    "module_name",
    (
        "cortex.apps.desktop_shell.dashboard",
        "cortex.apps.desktop_shell.connections",
        "cortex.apps.desktop_shell.settings",
        "cortex.apps.desktop_shell.onboarding",
    ),
)
def test_surface_uses_registry_tints_directly(module_name: str) -> None:
    """Every surface reads the label tints straight from the registry —
    no private ``_LABEL_*`` alias that a later edit could re-point."""
    import importlib

    from cortex.apps.desktop_shell.tokens import (
        CX_TEXT_SECONDARY,
        CX_TEXT_TERTIARY,
    )

    module = importlib.import_module(module_name)
    assert module.CX_TEXT_TERTIARY == CX_TEXT_TERTIARY
    assert module.CX_TEXT_SECONDARY == CX_TEXT_SECONDARY
    for alias in ("_LABEL_TERTIARY", "_LABEL_SECONDARY", "_LABEL", "_WINDOW_BG"):
        assert not hasattr(module, alias), (
            f"{module_name} carries a private {alias} alias — use the CX_* tokens"
        )
