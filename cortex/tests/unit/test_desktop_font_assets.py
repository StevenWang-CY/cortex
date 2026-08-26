"""Bundled desktop typography must register before UI construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.apps.desktop_shell import font_assets
from cortex.apps.desktop_shell.tokens import BRAND_DISPLAY_FONT


def test_source_font_assets_and_license_are_present() -> None:
    root = font_assets.font_asset_root()

    assert (root / "CormorantGaramond[wght].ttf").stat().st_size > 1_000_000
    assert (root / "CormorantGaramond-Italic[wght].ttf").stat().st_size > 500_000
    assert "SIL OPEN FONT LICENSE" in (root / "OFL.txt").read_text(
        encoding="utf-8"
    )


def test_font_registration_rejects_missing_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(font_assets, "_installed", False)
    monkeypatch.setattr(font_assets, "font_asset_root", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="required display font is missing"):
        font_assets.install_application_fonts()


def test_real_bundled_faces_register_expected_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(font_assets, "_installed", False)

    font_assets.install_application_fonts()

    from PySide6.QtGui import QFontDatabase

    assert BRAND_DISPLAY_FONT in QFontDatabase.families()
    assert app is not None
