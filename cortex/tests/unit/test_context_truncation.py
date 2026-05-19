"""Audit F29 — context truncation telemetry + UI affordance.

Pre-fix, ``_truncate_section`` silently dropped content. A user pasting
a 200-line traceback would unknowingly ship only the first 10 lines to
the LLM and wonder why the plan ignored the actual error on line 187.

The fix has three pieces:

1. ``_truncate_section`` returns ``(text, dropped_chars)`` instead of
   only the truncated text.
2. ``_enforce_token_budget`` aggregates per-section losses into a
   :class:`TruncationReport` and emits
   :data:`EventType.CONTEXT_TRUNCATED` once per actually-truncated call.
3. The planner stamps ``InterventionPlan.metadata["context_truncated_sections"]``
   and the overlay surfaces a "Show more context" affordance when that
   field is populated.

Test cases (6):

1. No truncation → no event, no metadata, no UI affordance.
2. Single section trimmed → event emitted + metadata field set.
3. Multiple sections trimmed → metadata list contains all of them.
4. Byte/token math in the report is correct relative to the input.
5. ``InterventionPlan.metadata`` round-trips the truncated-sections
   list through Pydantic.
6. Overlay-side: the affordance label is hidden when metadata is
   absent and visible when metadata is populated.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.intervention import InterventionPlan, UIPlan
from cortex.services.llm_engine.prompts import (
    _enforce_token_budget,
    _truncate_section,
    capture_truncation_report,
)

# ---------------------------------------------------------------------------
# 1. No truncation -> no event, no flag
# ---------------------------------------------------------------------------


def test_no_truncation_emits_no_event(caplog: pytest.LogCaptureFixture) -> None:
    """If the messages already fit the budget, the report is empty and
    no CONTEXT_TRUNCATED log line is emitted.
    """
    messages = [
        {"role": "system", "content": "short system"},
        {"role": "user", "content": "tiny user message"},
    ]
    with capture_truncation_report() as report:
        with caplog.at_level(logging.WARNING):
            result = _enforce_token_budget(messages, max_context_tokens=10_000)

    assert result == messages
    assert report.truncated is False
    assert report.sections_trimmed == []
    assert not any(
        EventType.CONTEXT_TRUNCATED.value in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 2. Single section trimmed -> event + flag
# ---------------------------------------------------------------------------


def test_single_section_trimmed_records_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A terminal-errors section that overflows triggers a single
    CONTEXT_TRUNCATED event and stamps ``terminal_errors`` on the
    report."""
    # Long terminal section. Each line ~20 chars × 2000 = 40k chars =
    # ~10k tokens, comfortably over the 1600-token budget below.
    long_terminal = "\n".join(
        f"err line {i:06d} body" for i in range(2000)
    )
    user = (
        "--- Terminal Errors ---\n"
        f"{long_terminal}\n"
        "--- end ---\n"
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user},
    ]
    with capture_truncation_report() as report:
        with caplog.at_level(logging.WARNING):
            _enforce_token_budget(messages, max_context_tokens=2_000)

    assert report.truncated is True
    assert "terminal_errors" in report.sections_trimmed
    # Exactly one CONTEXT_TRUNCATED log line per truncation call.
    matches = [
        r
        for r in caplog.records
        if EventType.CONTEXT_TRUNCATED.value in r.getMessage()
    ]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# 3. Multiple sections trimmed -> list contains all
# ---------------------------------------------------------------------------


def test_multiple_sections_trimmed_lists_all() -> None:
    """When budget pressure forces both terminal and code trimming, the
    report carries both section names in order."""
    long_terminal = "\n".join(f"err line {i}" for i in range(500))
    long_code = "\n".join(f"code line {i}" for i in range(500))
    user = (
        "Goal: pretend\n"
        "--- Terminal Errors ---\n"
        f"{long_terminal}\n"
        "--- Boundary ---\n"
        "Code:\n"
        f"{long_code}\n"
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user},
    ]
    with capture_truncation_report() as report:
        _enforce_token_budget(messages, max_context_tokens=2_000)

    assert "terminal_errors" in report.sections_trimmed
    assert "code" in report.sections_trimmed


# ---------------------------------------------------------------------------
# 4. Byte/token arithmetic in the report
# ---------------------------------------------------------------------------


def test_truncate_section_returns_dropped_chars_count() -> None:
    """``_truncate_section`` returns a tuple. ``dropped_chars`` reflects
    the bytes removed from the section, not the truncation-marker bytes
    added by the helper."""
    # 50 lines of body content. With ``max_lines=10`` and the
    # implementation's line accounting (the marker line "Code:" counts
    # as line 0 of the section), 9 body lines are kept and 41 are
    # dropped — exercise the actual contract.
    lines = [f"line-{i}" for i in range(50)]
    text = "Code:\n" + "\n".join(lines) + "\n--- end ---\n"
    truncated, dropped = _truncate_section(
        text, start_marker="Code:", max_lines=10
    )
    assert dropped > 0
    # Lower bound: 41 dropped lines × ~7 chars + 41 newlines = ~328 bytes.
    assert dropped >= 41 * 7
    assert "Code:" in truncated
    # The marker says how many lines were dropped — pin the contract.
    assert "lines truncated" in truncated


def test_report_aggregates_token_counts() -> None:
    """``original_tokens`` and ``truncated_tokens`` are populated when
    truncation actually occurs, and ``truncated_tokens`` is at most the
    original."""
    long_terminal = "\n".join(f"err {i}" for i in range(800))
    user = f"--- Terminal Errors ---\n{long_terminal}\n--- end ---\n"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user},
    ]
    with capture_truncation_report() as report:
        _enforce_token_budget(messages, max_context_tokens=1_500)

    assert report.original_tokens > 0
    assert report.truncated_tokens > 0
    assert report.truncated_tokens <= report.original_tokens


# ---------------------------------------------------------------------------
# 5. InterventionPlan.metadata round-trips through Pydantic
# ---------------------------------------------------------------------------


def test_intervention_plan_metadata_round_trip() -> None:
    plan = InterventionPlan(
        level="overlay_only",
        situation_summary="test",
        headline="Headline goes here",
        primary_focus="Focus on the failing test",
        micro_steps=["one", "two"],
        ui_plan=UIPlan(),
        metadata={"context_truncated_sections": ["terminal_errors", "code"]},
    )
    blob = plan.model_dump_json()
    rehydrated = InterventionPlan.model_validate_json(blob)
    assert rehydrated.metadata.get("context_truncated_sections") == [
        "terminal_errors",
        "code",
    ]

    # Default is an empty dict, so happy-path plans don't pay schema cost.
    bare = InterventionPlan(
        level="overlay_only",
        situation_summary="test",
        headline="Hi",
        primary_focus="x",
        micro_steps=["a"],
        ui_plan=UIPlan(),
    )
    assert bare.metadata == {}


# ---------------------------------------------------------------------------
# 6. Overlay surfaces the affordance only when metadata is populated
# ---------------------------------------------------------------------------


@pytest.fixture
def qt_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a Qt app and stub out the macOS-native chrome APIs.

    ``mac_native.apply_unified_titlebar`` and ``apply_vibrancy`` poke at
    the underlying NSWindow via objc bridges; in headless / offscreen
    test mode those bridges return invalid pointers and crash the
    interpreter. Stubbing them to no-ops keeps the affordance contract
    under test (visibility toggle) testable without the segfault path.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        mac_native, "apply_vibrancy", lambda *_a, **_kw: None
    )
    app = QApplication.instance() or QApplication([])
    yield app


def test_overlay_affordance_visibility_follows_metadata(qt_app) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    overlay = OverlayWindow()

    base_payload: dict[str, Any] = {
        "intervention_id": "int_test01",
        "headline": "Pause",
        "situation_summary": "Sit still",
        "primary_focus": "breathing",
        "micro_steps": ["breathe in", "breathe out"],
        "ui_plan": {"show_overlay": True},
        "level": "overlay_only",
        "causal_explanation": "",
    }

    # No metadata: affordance hidden.
    overlay.show_intervention(dict(base_payload))
    assert overlay._context_truncation_label.isHidden() is True

    # Empty metadata: still hidden.
    overlay.show_intervention(dict(base_payload, metadata={}))
    assert overlay._context_truncation_label.isHidden() is True

    # Empty list of sections: still hidden.
    overlay.show_intervention(
        dict(base_payload, metadata={"context_truncated_sections": []})
    )
    assert overlay._context_truncation_label.isHidden() is True

    # Populated list: visible + label quotes the section name.
    overlay.show_intervention(
        dict(
            base_payload,
            metadata={"context_truncated_sections": ["terminal_errors"]},
        )
    )
    assert overlay._context_truncation_label.isHidden() is False
    label_text = overlay._context_truncation_label.text()
    assert "terminal errors" in label_text
    assert "Show more context" in label_text

    overlay.close()
