"""P2-17: defaults.yaml must declare the three P0 phase-4a feature flags."""

from __future__ import annotations

from pathlib import Path

import yaml

_DEFAULTS = Path(__file__).resolve().parent.parent.parent / "libs" / "config" / "defaults.yaml"

_REQUIRED_FLAGS = [
    "enable_biology_break",
    "enable_auto_distraction_block",
    "enable_os_notifications",
]


def _load() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}


def test_intervention_section_exists() -> None:
    data = _load()
    assert "intervention" in data, "defaults.yaml must have an 'intervention' section"


def test_biology_break_flag_present() -> None:
    data = _load()
    assert "enable_biology_break" in data["intervention"], (
        "defaults.yaml[intervention] must contain enable_biology_break"
    )


def test_auto_distraction_block_flag_present() -> None:
    data = _load()
    assert "enable_auto_distraction_block" in data["intervention"], (
        "defaults.yaml[intervention] must contain enable_auto_distraction_block"
    )


def test_os_notifications_flag_present() -> None:
    data = _load()
    assert "enable_os_notifications" in data["intervention"], (
        "defaults.yaml[intervention] must contain enable_os_notifications"
    )


def test_flag_defaults_match_spec() -> None:
    """Confirm the defaults match the spec: bio=true, block=false, notif=true."""
    data = _load()
    iv = data["intervention"]
    assert iv["enable_biology_break"] is True
    assert iv["enable_auto_distraction_block"] is False
    assert iv["enable_os_notifications"] is True
