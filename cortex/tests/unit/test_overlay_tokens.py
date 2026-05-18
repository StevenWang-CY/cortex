"""Audit F47 — overlay HUD palette flows through tokens.py.

Pre-fix, ``overlay.py`` hardcoded its palette as inline ``QColor(...)``
literals — ``_ACCENT = QColor(217, 119, 87)`` etc. That meant a token
update in ``tokens.py`` did not propagate to the intervention overlay,
and the overlay's contrast / dark-mode behaviour drifted from the rest
of the desktop shell.

Test contract:

1. ``overlay.py`` no longer contains hex-string ``QColor("#...")`` literals.
2. ``overlay.py`` no longer contains inline ``QColor(<3-or-4 numeric
   tuple>)`` literals for the palette (we still permit
   ``QColor(*TOKEN_TUPLE)`` because the tuple is loaded from tokens.py).
3. ``tokens.py`` exposes ``TEXT_HUD_PRIMARY``, ``TEXT_HUD_SECONDARY``,
   ``TEXT_HUD_TERTIARY``, ``HUD_ACCENT`` and they match the prior
   hardcoded values.
4. The module-level ``_ACCENT``, ``_TEXT_PRIMARY``, ``_TEXT_SECONDARY``,
   ``_TEXT_TERTIARY`` resolve to the right RGBA on import.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


OVERLAY = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "desktop_shell"
    / "overlay.py"
)


def test_overlay_has_no_hex_qcolor_literals() -> None:
    source = OVERLAY.read_text(encoding="utf-8")
    matches = re.findall(r'QColor\(\s*"#[0-9A-Fa-f]+"\s*\)', source)
    assert not matches, (
        f"overlay.py must not contain hex-string QColor literals — "
        f"move them to tokens.py. Found: {matches}"
    )


def test_overlay_palette_loads_from_tokens() -> None:
    """The four palette globals must equal the token tuple values
    they're built from (not stray numeric literals)."""
    from cortex.apps.desktop_shell import overlay
    from cortex.apps.desktop_shell.tokens import (
        HUD_ACCENT,
        TEXT_HUD_PRIMARY,
        TEXT_HUD_SECONDARY,
        TEXT_HUD_TERTIARY,
    )

    def rgba(c) -> tuple[int, int, int, int]:
        return (c.red(), c.green(), c.blue(), c.alpha())

    assert rgba(overlay._ACCENT) == HUD_ACCENT
    assert rgba(overlay._TEXT_PRIMARY) == TEXT_HUD_PRIMARY
    assert rgba(overlay._TEXT_SECONDARY) == TEXT_HUD_SECONDARY
    assert rgba(overlay._TEXT_TERTIARY) == TEXT_HUD_TERTIARY


def test_tokens_expose_hud_palette() -> None:
    """The HUD palette token entries must exist and have the documented
    RGBA values (the alpha values are calibrated for WCAG AA contrast
    on the HUD vibrancy material)."""
    from cortex.apps.desktop_shell.tokens import (
        HUD_ACCENT,
        TEXT_HUD_PRIMARY,
        TEXT_HUD_SECONDARY,
        TEXT_HUD_TERTIARY,
    )

    assert TEXT_HUD_PRIMARY == (255, 255, 255, 235)
    assert TEXT_HUD_SECONDARY == (255, 255, 255, 150)
    assert TEXT_HUD_TERTIARY == (255, 255, 255, 100)
    # Brand terracotta — preserved across refactor.
    assert HUD_ACCENT[:3] == (217, 119, 87)


def test_overlay_palette_section_no_inline_rgb_tuples() -> None:
    """The lines around the palette declaration (the citation range in
    F47) must not include inline RGB tuples. We grep the first 80 lines
    (the palette block) for QColor with numeric literals; QColor(*TOKEN)
    is fine because it splatters a tuple from tokens.py."""
    source_lines = OVERLAY.read_text(encoding="utf-8").splitlines()
    # Only the palette block — lines 50-80 in the post-refactor file.
    block = "\n".join(source_lines[45:80])
    inline = re.findall(r"QColor\(\s*\d+\s*,", block)
    assert not inline, (
        "Palette section must use QColor(*TOKEN) — not inline RGB "
        f"literals. Found: {inline}"
    )
