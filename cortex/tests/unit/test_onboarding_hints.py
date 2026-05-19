"""Phase J-1 — Onboarding refinement: Continuity callout + Why expanders.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_onboarding_hints.py``

The onboarding wizard now (1) detects a paired iPhone / iPad Continuity
Camera and surfaces an inline "we will skip your iPhone camera" callout on
the Camera card, and (2) renders a "Why we need this" expand-on-click
chevron on every card with rationale copy keyed by step id.

These tests exercise the real Qt widget under the offscreen platform —
the prior wave's onboarding tests already proved this works for the
state-marker code path; we extend the same harness for the visual
affordances.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Drop any stale PySide6 mocks installed by other test modules.
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
def wizard(qapp, monkeypatch):
    """Build an OnboardingWindow without the macOS-only chrome calls."""
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import onboarding as onb_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    # Force a deterministic "no Continuity Camera" state for the baseline
    # tests; the dedicated test for the callout monkeypatches this back.
    monkeypatch.setattr(onb_mod, "_detect_continuity_camera", lambda: False)

    w = onb_mod.OnboardingWindow()
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


def test_every_card_has_why_expander(wizard):
    """Cards 1-4 each carry a "Why?" toggle button and a hidden-by-default
    rationale body. Toggling flips the visibility flag and updates the
    button text. Qt's ``isVisible()`` reports the effective on-screen
    state; under offscreen the wizard window itself is never realised so
    we read ``isVisibleTo(card)`` which reflects the explicit setVisible
    call (the contract this test owns)."""
    cards = [
        wizard._camera_step,
        wizard._accessibility_step,
        wizard._llm_step,
        wizard._extensions_step,
    ]
    for card in cards:
        btn = getattr(card, "_cortex_why_btn", None)
        body = getattr(card, "_cortex_why_body", None)
        assert btn is not None, "Why button missing on card"
        assert body is not None, "Why body missing on card"
        assert btn.isCheckable(), "Why button must be checkable"
        assert not body.isVisibleTo(card), "Why body starts collapsed"
        assert "Why" in btn.text()
        # Toggle open.
        btn.setChecked(True)
        assert body.isVisibleTo(card)
        assert "⌄" in btn.text() or btn.text().endswith("⌄")
        # Toggle closed.
        btn.setChecked(False)
        assert not body.isVisibleTo(card)


def test_why_copy_is_substantive(wizard):
    """Each rationale paragraph carries the spec-defined copy. Pinning the
    keyword guards against future copy edits that silently drop the
    rationale (e.g. an accidental empty-string default)."""
    pinned = {
        wizard._camera_step: "facial cues",
        wizard._accessibility_step: "system-wide events",
        wizard._llm_step: "macOS Keychain",
        wizard._extensions_step: "browser",
    }
    for card, keyword in pinned.items():
        body = getattr(card, "_cortex_why_body", None)
        assert body is not None
        assert keyword in body.text(), (
            f"rationale must mention {keyword!r}; got: {body.text()!r}"
        )


def test_continuity_callout_appears_when_iphone_present(qapp, monkeypatch):
    """When AVFoundation lists an iPhone Continuity Camera, the Camera card
    renders the skip callout inline. Without one, the callout is absent."""
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import onboarding as onb_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    # Force the detection helper to claim a paired iPhone is present.
    monkeypatch.setattr(onb_mod, "_detect_continuity_camera", lambda: True)

    w = onb_mod.OnboardingWindow()
    try:
        callout = getattr(w._camera_step, "_cortex_continuity_callout", None)
        assert callout is not None, "Continuity callout missing when iPhone present"
        assert "iPhone" in callout.text()
        assert "MacBook" in callout.text()
        # The other cards never carry a callout.
        assert getattr(w._accessibility_step, "_cortex_continuity_callout", None) is None
        assert getattr(w._llm_step, "_cortex_continuity_callout", None) is None
    finally:
        try:
            w.deleteLater()
        except RuntimeError:
            pass


def test_continuity_callout_absent_when_no_iphone(wizard):
    """Default fixture sets _detect_continuity_camera → False; callout absent."""
    assert getattr(wizard._camera_step, "_cortex_continuity_callout", None) is None


def test_detect_continuity_camera_keywords(monkeypatch):
    """The detector returns True for any AVFoundation device whose name
    contains an iPhone / iPad / Continuity keyword."""
    from cortex.apps.desktop_shell import onboarding as onb_mod
    from cortex.services.capture_service import webcam as webcam_mod

    monkeypatch.setattr(
        webcam_mod,
        "_list_macos_video_device_names",
        lambda: ["FaceTime HD Camera"],
    )
    assert onb_mod._detect_continuity_camera() is False

    monkeypatch.setattr(
        webcam_mod,
        "_list_macos_video_device_names",
        lambda: ["FaceTime HD Camera", "Chuyue's iPhone"],
    )
    assert onb_mod._detect_continuity_camera() is True

    monkeypatch.setattr(
        webcam_mod,
        "_list_macos_video_device_names",
        lambda: ["iPad Pro"],
    )
    assert onb_mod._detect_continuity_camera() is True


def test_why_button_has_accessible_name(wizard):
    """Phase J-1 + audit-w2: each Why expander button carries an accessible
    name so VoiceOver announces it semantically rather than as "button"."""
    for card in (
        wizard._camera_step,
        wizard._accessibility_step,
        wizard._llm_step,
        wizard._extensions_step,
    ):
        btn = getattr(card, "_cortex_why_btn", None)
        assert btn is not None
        name = btn.accessibleName()
        assert name, "Why button missing accessible name"
        assert "Why" in name
