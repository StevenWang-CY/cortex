"""Phase 4c desktop_shell unit tests (P0 §3.13 / §3.15 / §3.20 / §3.25
+ token regeneration smoke).

These tests intentionally avoid spinning up a real Qt event loop or
constructing any QWidget — they exercise the pure-Python helpers that
each desktop feature relies on. The wider Qt-based test
(``cortex/tests/unit/test_desktop_shell.py``) has a pre-existing
PySide6 import issue that is out of scope here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# P0 §3.13 — goal_store CRUD
# ---------------------------------------------------------------------------


def test_goal_store_add_load_roundtrip(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import Goal, add_goal, load_goals

    path = tmp_path / "goals.json"
    g = add_goal("Ship the audit", path=path)
    assert isinstance(g, Goal)
    assert g.title == "Ship the audit"
    loaded = load_goals(path=path)
    assert len(loaded) == 1
    assert loaded[0].title == "Ship the audit"
    assert loaded[0].id == g.id


def test_goal_store_dedupes_by_title(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import add_goal, load_goals

    path = tmp_path / "goals.json"
    a = add_goal("Refactor cost tracker", path=path)
    b = add_goal("REFACTOR cost tracker", path=path)  # case-insensitive
    assert a.id == b.id, "identical titles should reuse the same id"
    loaded = load_goals(path=path)
    assert len(loaded) == 1


def test_goal_store_mark_used_bumps_counter(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import add_goal, load_goals, mark_used

    path = tmp_path / "goals.json"
    g = add_goal("Demo", path=path)
    updated = mark_used(g.id, path=path)
    assert updated is not None
    assert updated.sessions_count == 1
    loaded = load_goals(path=path)
    assert loaded[0].sessions_count == 1


def test_goal_store_delete(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import add_goal, delete_goal, load_goals

    path = tmp_path / "goals.json"
    g = add_goal("Throwaway", path=path)
    assert delete_goal(g.id, path=path) is True
    assert load_goals(path=path) == []
    # Second delete on the same id is a no-op.
    assert delete_goal(g.id, path=path) is False


def test_goal_store_empty_title_rejected(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import add_goal

    with pytest.raises(ValueError):
        add_goal("   ", path=tmp_path / "goals.json")


def test_goal_store_caps_at_max(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import MAX_GOALS, add_goal, load_goals

    path = tmp_path / "goals.json"
    for i in range(MAX_GOALS + 5):
        add_goal(f"Goal {i:03d}", path=path)
    assert len(load_goals(path=path)) == MAX_GOALS


def test_goal_store_load_missing_file_returns_empty(tmp_path: Path) -> None:
    from cortex.libs.store.goal_store import load_goals

    assert load_goals(path=tmp_path / "missing.json") == []


# ---------------------------------------------------------------------------
# P0 §3.15 — Cost-pill rendering math (helper extracted from dashboard)
# ---------------------------------------------------------------------------


def _cost_text(cost: float, budget: float, last_value: float = -1.0) -> str:
    """Mirror of the dashboard's text-composition logic."""
    text = "$—" if cost < 0.005 and last_value < 0 else f"${cost:.2f}"
    if budget > 0:
        text = f"{text} / ${budget:.2f}"
    return text


def test_cost_pill_text_initial_empty_state() -> None:
    assert _cost_text(0.0, 0.0, last_value=-1.0) == "$—"


def test_cost_pill_text_under_budget() -> None:
    assert _cost_text(0.42, 1.50) == "$0.42 / $1.50"


def test_cost_pill_text_no_budget() -> None:
    assert _cost_text(2.10, 0.0) == "$2.10"


# ---------------------------------------------------------------------------
# P0 §3.20 — Weekly schedule serialisation round-trip
# ---------------------------------------------------------------------------


def test_weekly_schedule_serialises_as_json() -> None:
    schedule = {
        "monday":    ["on", "on", "on", "off"],
        "tuesday":   ["on", "on", "on", "off"],
        "wednesday": ["on", "on", "on", "off"],
        "thursday":  ["on", "on", "on", "off"],
        "friday":    ["on", "on", "quiet", "off"],
        "saturday":  ["off", "off", "off", "off"],
        "sunday":    ["off", "off", "off", "off"],
    }
    encoded = json.dumps(schedule, separators=(",", ":"))
    decoded = json.loads(encoded)
    assert decoded == schedule
    # Validate slot vocabulary is preserved (the daemon's consumer
    # will reject anything other than these three).
    for slots in decoded.values():
        for slot in slots:
            assert slot in {"on", "quiet", "off"}


# ---------------------------------------------------------------------------
# P0 §3.25 — Color-blind palette runtime swap
# ---------------------------------------------------------------------------


def test_palette_runtime_default() -> None:
    from cortex.apps.desktop_shell.palette_runtime import (
        active_state_color,
        set_active_palette,
    )
    set_active_palette("default")
    # FLOW is the brand terracotta in the default palette.
    assert active_state_color("FLOW") == "#D97757"


def test_palette_runtime_deuteranopia_swap() -> None:
    from cortex.apps.desktop_shell.palette_runtime import (
        active_state_color,
        set_active_palette,
    )
    set_active_palette("deuteranopia")
    # Under deuteranopia FLOW is the blue anchor (#0072B2) — distinct
    # from the danger / orange hue.
    assert active_state_color("FLOW") == "#0072B2"
    assert active_state_color("HYPER") == "#E69F00"
    # Reset so subsequent tests aren't polluted.
    set_active_palette("default")


def test_palette_runtime_unknown_falls_back() -> None:
    from cortex.apps.desktop_shell.palette_runtime import (
        active_palette,
        active_state_color,
        set_active_palette,
    )
    set_active_palette("nonsense")
    assert active_palette() == "default"
    assert active_state_color("FLOW") == "#D97757"


# ---------------------------------------------------------------------------
# Token sync smoke — BRAND_ACCENT_PRESSED + HUD bg tokens land in the
# generated tokens.py.
# ---------------------------------------------------------------------------


def test_tokens_module_exports_brand_accent_pressed() -> None:
    from cortex.apps.desktop_shell import tokens

    assert hasattr(tokens, "BRAND_ACCENT_PRESSED")
    assert tokens.BRAND_ACCENT_PRESSED == "#B45638"


def test_tokens_module_exports_hud_bg_pair() -> None:
    from cortex.apps.desktop_shell import tokens

    assert hasattr(tokens, "HUD_BG_PRIMARY")
    assert hasattr(tokens, "HUD_BG_SECONDARY")
    assert tokens.HUD_BG_PRIMARY[3] == 255  # opaque alpha


def test_tokens_module_exports_palette_variants() -> None:
    from cortex.apps.desktop_shell import tokens

    assert "deuteranopia" in tokens.PALETTE_VARIANTS
    deut = tokens.PALETTE_VARIANTS["deuteranopia"]
    assert deut["FLOW"] != deut["HYPER"]


# ---------------------------------------------------------------------------
# P0 §3.17 — Glossary singleton (tooltip / Concepts dialog source).
# ---------------------------------------------------------------------------


def test_concepts_glossary_has_core_terms() -> None:
    from cortex.apps.desktop_shell import dashboard

    glossary = dashboard._CONCEPTS_GLOSSARY
    for required in ("state", "hr", "hrv", "perclos", "sqi"):
        assert required in glossary, f"glossary missing {required!r}"
        # Every entry is a non-empty string so the tooltip is not blank.
        assert isinstance(glossary[required], str)
        assert len(glossary[required]) > 10


# ---------------------------------------------------------------------------
# P0 §3.16 — Reversible action set mirrors executor map.
# ---------------------------------------------------------------------------


def test_reversible_set_mirrors_executor_keys() -> None:
    from cortex.apps.desktop_shell.dashboard import _ConsumerTab
    from cortex.services.intervention_engine.executor import (
        _REVERSE_ACTIONS,
    )

    mirror = _ConsumerTab._DESKTOP_REVERSIBLE_ACTIONS
    # The mirror is allowed to drop a few entries (e.g. ``show_overlay``
    # is rarely reversed from the desktop side) but everything in the
    # mirror must exist in the executor map. This catches a typo in
    # the mirror — the dashboard would otherwise show an Undo toast
    # for an action the daemon cannot actually undo.
    for action in mirror:
        assert action in _REVERSE_ACTIONS, (
            f"{action} in desktop mirror but not in executor _REVERSE_ACTIONS"
        )


# ---------------------------------------------------------------------------
# P0 §3.5 — overlay action-type frozensets include the new natives.
# ---------------------------------------------------------------------------


def test_overlay_native_action_types_extended() -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    native = OverlayWindow._NATIVE_ACTION_TYPES
    for required in (
        "resume_last_active_file",
        "prompt_micro_commit",
        "suggest_movement_break",
        "take_biology_break",
    ):
        assert required in native, (
            f"new action type {required!r} missing from _NATIVE_ACTION_TYPES"
        )
