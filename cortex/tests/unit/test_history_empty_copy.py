"""P2-8 — HistoryTab empty-state copy keyed by context.

Two distinct empty-state strings are required:

1. Top-level history (no sessions yet, ``_TodayPanel``):
   "No sessions yet — your history will appear here as you use Cortex."

2. Per-session detail (session exists but no distractions/interventions,
   ``_DetailPanel._populate_distractions``):
   "No interventions in this session — focus held steady."

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_history_empty_copy.py``
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
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def history_tab(qapp):
    from cortex.apps.desktop_shell import history_tab as ht_mod

    tab = ht_mod.HistoryTab()
    yield tab
    try:
        tab.deleteLater()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Test 1: top-level empty state copy (no sessions yet)
# ---------------------------------------------------------------------------

TOP_LEVEL_EMPTY_COPY = (
    "No sessions yet — your history will appear here as you use Cortex."
)
DETAIL_EMPTY_COPY = "No interventions in this session — focus held steady."
OLD_COPY = "None — nice work staying focused."


def test_top_level_empty_copy(history_tab):
    """_TodayPanel._empty_label uses the top-level "no sessions" copy."""
    panel = history_tab._today_panel
    label = panel._empty_label
    assert label.text() == TOP_LEVEL_EMPTY_COPY, (
        f"Top-level empty copy mismatch: {label.text()!r}"
    )


def test_top_level_empty_copy_not_old_text(history_tab):
    """The old 'None — nice work' copy must not appear on the top-level label."""
    label = history_tab._today_panel._empty_label
    assert OLD_COPY not in label.text(), (
        f"Old copy must have been replaced: {label.text()!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: per-session detail empty state copy (no distractions this session)
# ---------------------------------------------------------------------------

def _collect_labels(widget) -> list[str]:
    """Recursively collect text from all QLabel children."""
    texts: list[str] = []
    try:
        for child in widget.children():
            if isinstance(child, QLabel):
                texts.append(child.text())
            texts.extend(_collect_labels(child))
    except Exception:
        pass
    return texts


def test_detail_empty_copy_when_no_distractions(history_tab):
    """When a session detail has no distraction domains, show the per-session
    empty copy rather than the old 'None — nice work' copy."""
    detail = history_tab._detail_panel
    # _populate_distractions is a method on _DetailPanel.
    detail._populate_distractions([])

    texts = _collect_labels(detail)
    assert any(DETAIL_EMPTY_COPY in t for t in texts), (
        f"Per-session empty copy not found. Labels: {texts!r}"
    )
    assert not any(OLD_COPY in t for t in texts), (
        f"Old copy must not appear in detail panel. Labels: {texts!r}"
    )


def test_detail_empty_copy_not_old_text_with_no_domains(history_tab):
    """Explicit check that the old 'None — nice work' string is gone."""
    detail = history_tab._detail_panel
    detail._populate_distractions([])

    texts = _collect_labels(detail)
    assert not any(OLD_COPY in t for t in texts), (
        f"'None — nice work staying focused.' must be replaced: {texts!r}"
    )
