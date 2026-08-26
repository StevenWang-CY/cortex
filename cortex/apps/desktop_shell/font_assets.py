"""Registration boundary for Cortex's bundled display typeface."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from cortex.apps.desktop_shell.tokens import BRAND_DISPLAY_FONT

logger = logging.getLogger(__name__)

_FONT_FILES = (
    "CormorantGaramond[wght].ttf",
    "CormorantGaramond-Italic[wght].ttf",
)
_installed = False


def font_asset_root() -> Path:
    """Return the source or PyInstaller resource directory for fonts."""

    if bool(getattr(sys, "frozen", False)):
        raw_root = getattr(sys, "_MEIPASS", None)
        if not isinstance(raw_root, str) or not raw_root:
            raise RuntimeError("frozen Cortex process has no resource root")
        return Path(raw_root) / "cortex" / "assets" / "fonts"
    return Path(__file__).resolve().parents[2] / "assets" / "fonts"


def install_application_fonts() -> None:
    """Install and validate every bundled face before constructing surfaces.

    A missing display face must never silently change the product typography.
    Qt returns ``-1`` when a font is invalid and exposes the registered family
    names for successful files, so startup can fail visibly through the
    standard bootstrap diagnostic instead of falling back unpredictably.
    """

    global _installed
    if _installed:
        return

    from PySide6.QtGui import QFontDatabase

    root = font_asset_root()
    installed_families: set[str] = set()
    for filename in _FONT_FILES:
        path = root / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"required display font is missing or empty: {path}")
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            raise RuntimeError(f"Qt rejected bundled display font: {path}")
        installed_families.update(QFontDatabase.applicationFontFamilies(font_id))

    if BRAND_DISPLAY_FONT not in installed_families:
        raise RuntimeError(
            "bundled display fonts did not register the expected family "
            f"{BRAND_DISPLAY_FONT!r}; registered={sorted(installed_families)!r}"
        )
    _installed = True
    logger.info(
        "startup.fonts_ready family=%s files=%d",
        BRAND_DISPLAY_FONT,
        len(_FONT_FILES),
    )


__all__ = ["font_asset_root", "install_application_fonts"]
