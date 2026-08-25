"""Safety-sensitive intervention defaults remain explicit and conservative."""

from __future__ import annotations

from pathlib import Path

import yaml

_DEFAULTS = Path(__file__).resolve().parent.parent.parent / "libs" / "config" / "defaults.yaml"

_REQUIRED_FLAGS = [
    "execution_mode",
    "enable_focus_break_reminders",
    "enable_auto_distraction_block",
    "enable_os_notifications",
]


def _load() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}


def test_intervention_section_exists() -> None:
    data = _load()
    assert "intervention" in data, "defaults.yaml must have an 'intervention' section"


def test_focus_break_flag_present() -> None:
    data = _load()
    assert "enable_focus_break_reminders" in data["intervention"], (
        "defaults.yaml[intervention] must contain enable_focus_break_reminders"
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
    """Confirm fresh installs default to presentation-only authority."""
    data = _load()
    iv = data["intervention"]
    assert iv["enable_focus_break_reminders"] is False
    assert iv["execution_mode"] == "suggest_only"
    assert iv["enable_auto_distraction_block"] is False
    assert iv["enable_os_notifications"] is True
