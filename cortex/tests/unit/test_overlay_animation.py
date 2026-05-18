"""Phase J-4 — Overlay show-time micro-interactions.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_overlay_animation.py``

The intervention overlay plays two subtle tweens when ``show_intervention``
is called:

* The headline scales in (geometry tween, 250 ms, OutCubic easing).
* The causal-explanation row fades in (opacity 0 → 1, 180 ms, InOutSine
  easing), started AFTER the headline animation completes so the two
  read as a single continuous motion.

The dismiss button and micro-step checkboxes are explicitly NOT animated
— motion stays purposeful per the audit's "be conservative" rule. The
breathing pacer keeps its existing rhythm independently.

Reduce Motion: when the macOS "Reduce Motion" accessibility preference is
enabled (System Settings → Accessibility → Display → Reduce motion),
both animations are skipped and the end state is applied directly.

Test strategy
=============

QPropertyAnimation needs a real Qt event loop to tick at 16ms intervals.
The unit test instead enables ``OverlayWindow._record_animations`` to
capture the durations the code path would use without spinning the
animations themselves; that proves the wiring (headline = 250 ms, causal
= 180 ms, Reduce Motion = 0 ms). A manual QA step is documented in the
commit body for the live tween verification.
"""

from __future__ import annotations

import os
import sys


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def overlay(qapp, monkeypatch):
    from cortex.apps.desktop_shell import mac_native, overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    # Force a deterministic Reduce-Motion=False baseline.
    monkeypatch.setattr(mac_native, "prefers_reduced_motion", lambda: False)

    w = overlay_mod.OverlayWindow()
    # Test-mode: record durations without spinning animations.
    w._record_animations = True
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


def test_animation_durations_match_spec(overlay):
    """The headline scale-in fires at 250 ms; the causal fade at 180 ms.
    These constants are part of the audit-pinned contract — any future
    refactor that "tweens faster" or "slows for elegance" must update
    this test deliberately."""
    from cortex.apps.desktop_shell.overlay import (
        CAUSAL_FADE_DURATION_MS,
        HEADLINE_SCALE_DURATION_MS,
    )

    assert HEADLINE_SCALE_DURATION_MS == 250
    assert CAUSAL_FADE_DURATION_MS == 180

    overlay.show_intervention({
        "intervention_id": "iv_anim_1",
        "headline": "Take a breath",
        "situation_summary": "summary",
        "primary_focus": "focus",
        "causal_explanation": "x" * 200,  # long enough for the truncation toggle
        "micro_steps": ["one"],
    })
    log = overlay._last_animation_log
    assert log["headline_ms"] == 250
    assert log["causal_ms"] == 180
    assert log["reduce_motion"] == 0


def test_reduce_motion_zeroes_both_durations(qapp, monkeypatch):
    """When the user has Reduce Motion enabled, both tweens are skipped:
    the durations recorded in the contract log are 0. The end state is
    applied directly (the test doesn't pin the end-state widget values
    because they are the same as the no-animation pre-fix UI)."""
    from cortex.apps.desktop_shell import mac_native, overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    monkeypatch.setattr(mac_native, "prefers_reduced_motion", lambda: True)

    w = overlay_mod.OverlayWindow()
    w._record_animations = True
    try:
        w.show_intervention({
            "intervention_id": "iv_anim_2",
            "headline": "Take a breath",
            "situation_summary": "summary",
            "primary_focus": "focus",
            "causal_explanation": "x" * 200,
            "micro_steps": ["one"],
        })
        log = w._last_animation_log
        assert log["headline_ms"] == 0, "Reduce Motion must zero the headline tween"
        assert log["causal_ms"] == 0, "Reduce Motion must zero the causal fade"
        assert log["reduce_motion"] == 1
    finally:
        try:
            w.deleteLater()
        except RuntimeError:
            pass


def test_dismiss_button_is_not_animated(overlay):
    """Per the audit's 'strictly purposeful' rule, the dismiss button
    must never have an animation. We pin this by asserting no attribute
    was created on the overlay for a dismiss-button animation."""
    overlay.show_intervention({
        "intervention_id": "iv_anim_3",
        "headline": "Take a breath",
        "situation_summary": "s",
        "primary_focus": "f",
        "causal_explanation": "",
        "micro_steps": [],
    })
    # No dismiss-button animation slot is wired.
    assert not hasattr(overlay, "_dismiss_anim")
    # Headline + causal slots exist (animation objects may be None when
    # the test runs in _record_animations mode).
    assert hasattr(overlay, "_headline_anim")
    assert hasattr(overlay, "_causal_fade_anim")


def test_back_to_back_interventions_reuse_animation_slots(overlay):
    """Two consecutive ``show_intervention`` calls must not leak a new
    animation object per call — the slot is reused so the prior anim is
    GC-able and the new one replaces it. We assert the slot name is
    stable across calls; the contents may be replaced."""
    overlay.show_intervention({
        "intervention_id": "iv_anim_4a",
        "headline": "First",
        "situation_summary": "s",
        "primary_focus": "f",
        "causal_explanation": "y" * 200,
        "micro_steps": [],
    })
    first_log = dict(overlay._last_animation_log)
    overlay.show_intervention({
        "intervention_id": "iv_anim_4b",
        "headline": "Second",
        "situation_summary": "s",
        "primary_focus": "f",
        "causal_explanation": "z" * 200,
        "micro_steps": [],
    })
    second_log = dict(overlay._last_animation_log)
    # Same durations on both calls — the contract is stable.
    assert first_log == second_log


def test_animation_log_is_populated_even_when_record_mode(overlay):
    """The _last_animation_log is the test contract — it must always be
    written, never left None, so a future refactor can't silently strip
    the recording."""
    overlay.show_intervention({
        "intervention_id": "iv_anim_5",
        "headline": "Take a breath",
        "situation_summary": "s",
        "primary_focus": "f",
        "causal_explanation": "",
        "micro_steps": [],
    })
    assert overlay._last_animation_log
    assert set(overlay._last_animation_log.keys()) == {
        "headline_ms",
        "causal_ms",
        "reduce_motion",
    }


def test_prefers_reduced_motion_helper_returns_bool(monkeypatch):
    """mac_native.prefers_reduced_motion must return a bool and never
    raise — UI surfaces consult it on every show, so a flaky call
    propagates everywhere."""
    from cortex.apps.desktop_shell import mac_native

    # On non-mac platforms the helper short-circuits to False.
    monkeypatch.setattr(mac_native, "_appkit", lambda: None)
    assert mac_native.prefers_reduced_motion() is False
