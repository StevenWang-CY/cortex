"""P0 §3.25 — Runtime palette swap for color-blind accessibility.

The static :data:`cortex.apps.desktop_shell.tokens.STATE_COLORS` is the
default red/green/blue/orange production palette. End users with a
color-vision deficiency can opt into one of the additional variants
shipped in :data:`cortex.apps.desktop_shell.tokens.PALETTE_VARIANTS`
(``deuteranopia`` / ``protanopia`` / ``tritanopia``).

We don't try to mutate the auto-generated tokens module at import time —
that would scramble static analysis and unit tests. Instead the
dashboard, browser extension, and overlay all call into
:func:`active_state_color` whenever they need a state colour, which
returns the palette-aware value. The active palette key is loaded from
QSettings on first call and cached; calling :func:`set_active_palette`
flushes the cache and persists the new key.
"""

from __future__ import annotations

import logging
from typing import Final

from cortex.apps.desktop_shell.tokens import PALETTE_VARIANTS, STATE_COLORS

logger = logging.getLogger(__name__)

# Persistent key in QSettings (cortex/desktop scope). The runtime
# loader does not actually hit QSettings (the tokens module is too low
# in the import graph to depend on Qt); the host applies the setting
# via :func:`set_active_palette` at startup.
_active_palette: str = "default"

VALID_PALETTES: Final[tuple[str, ...]] = (
    "default",
    "deuteranopia",
    "protanopia",
    "tritanopia",
)


def active_palette() -> str:
    """Return the currently active palette key."""
    return _active_palette


def set_active_palette(palette: str) -> None:
    """Swap the runtime palette. Unknown keys silently fall back to
    ``default`` so an out-of-date QSettings entry cannot break the UI.
    """
    global _active_palette
    if palette not in VALID_PALETTES:
        logger.debug(
            "unknown palette %r; falling back to default", palette,
        )
        _active_palette = "default"
    else:
        _active_palette = palette


def active_state_color(state: str) -> str:
    """Return the hex colour for ``state`` under the active palette.

    Unknown states or missing palette entries fall through to the
    default palette so callers always get a printable hex string.
    """
    state_key = (state or "").upper()
    variant = PALETTE_VARIANTS.get(_active_palette, {}) if _active_palette != "default" else {}
    if state_key in variant:
        return variant[state_key]
    return STATE_COLORS.get(state_key, "#999999")


__all__ = [
    "VALID_PALETTES",
    "active_palette",
    "active_state_color",
    "set_active_palette",
]
