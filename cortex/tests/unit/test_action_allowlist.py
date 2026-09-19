"""Audit F10 — executor-safety allowlist on LLM-emitted actions.

Two-layer defence:

1. **Pydantic validators** on ``SuggestedAction`` reject obviously-bad
   shapes at parse time — non-http(s) URL schemes, negative ``tab_index``,
   newlines in ``search_error`` targets, oversized ``target`` strings.
2. **``filter_unsafe_actions``** runs after enrichment and drops actions
   the schema cannot catch: ``tab_index`` upper-bound (depends on the
   live tab count), post-mutation re-checks for ``open_url`` scheme.

Every reject path emits a structured ``INTERVENTION_ACTION_REJECTED``
log line carrying the active correlation id so operators can audit.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SuggestedAction,
    UIPlan,
)
from cortex.services.llm_engine.parser import filter_unsafe_actions

# ---------------------------------------------------------------------------
# Layer 1 — Pydantic validators
# ---------------------------------------------------------------------------


def test_open_url_rejects_javascript_scheme() -> None:
    with pytest.raises(ValidationError) as exc:
        SuggestedAction(
            action_type="open_url",
            target="javascript:alert(1)",
            label="Open link",
        )
    assert "http or https" in str(exc.value)


def test_open_url_rejects_data_uri() -> None:
    with pytest.raises(ValidationError):
        SuggestedAction(
            action_type="open_url",
            target="data:text/html,<script>alert(1)</script>",
            label="Open link",
        )


def test_open_url_rejects_file_scheme() -> None:
    with pytest.raises(ValidationError):
        SuggestedAction(
            action_type="open_url",
            target="file:///etc/passwd",
            label="Open link",
        )


def test_open_url_rejects_no_hostname() -> None:
    with pytest.raises(ValidationError) as exc:
        SuggestedAction(
            action_type="open_url",
            target="https://",
            label="Open link",
        )
    # Either "hostname" or "http or https" is acceptable depending on parser
    assert "hostname" in str(exc.value) or "http or https" in str(exc.value)


def test_open_url_accepts_https() -> None:
    a = SuggestedAction(
        action_type="open_url",
        target="https://docs.python.org/3/library/asyncio.html",
        label="Open docs",
    )
    assert a.target.startswith("https://")


def test_open_url_accepts_http() -> None:
    SuggestedAction(
        action_type="open_url",
        target="http://localhost:8080",
        label="Open link",
    )


def test_open_url_empty_target_allowed_at_parse_time() -> None:
    """Enrichment may fill in the URL later; parse must not block an
    empty placeholder. The runtime filter still drops empty-target
    open_url actions."""
    SuggestedAction(
        action_type="open_url",
        target="",
        label="Open something",
    )


def test_search_error_rejects_newlines() -> None:
    with pytest.raises(ValidationError) as exc:
        SuggestedAction(
            action_type="search_error",
            target="TypeError: foo\nrm -rf /",
            label="Search error",
        )
    assert "line breaks" in str(exc.value)


def test_search_error_target_length_cap_200() -> None:
    long_q = "x" * 201
    with pytest.raises(ValidationError) as exc:
        SuggestedAction(
            action_type="search_error",
            target=long_q,
            label="Search error",
        )
    assert "too long" in str(exc.value)


def test_tab_index_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        SuggestedAction(
            action_type="close_tab",
            tab_index=-1,
            label="Close",
        )


def test_tab_index_none_allowed_for_non_tab_actions() -> None:
    SuggestedAction(
        action_type="copy_to_clipboard",
        target="hello",
        label="Copy",
        tab_index=None,
    )


# ---------------------------------------------------------------------------
# Layer 2 — filter_unsafe_actions
# ---------------------------------------------------------------------------


def _plan_with_actions(*actions: SuggestedAction) -> InterventionPlan:
    return InterventionPlan(
        level="overlay_only",
        situation_summary="t",
        headline="h",
        primary_focus="p",
        micro_steps=["s"],
        ui_plan=UIPlan(),
        suggested_actions=list(actions),
    )


def test_filter_drops_tab_index_at_or_above_tab_count() -> None:
    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=0, label="Close 0"),
        SuggestedAction(action_type="close_tab", tab_index=4, label="Close 4"),
        SuggestedAction(action_type="close_tab", tab_index=99, label="Close 99"),
    )
    filtered = filter_unsafe_actions(plan, tab_count=5)
    kept_indices = [a.tab_index for a in filtered.suggested_actions]
    assert kept_indices == [0, 4]


def test_filter_drops_tab_action_with_no_tab_index() -> None:
    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=None, label="?"),
    )
    filtered = filter_unsafe_actions(plan, tab_count=3)
    assert filtered.suggested_actions == []


def test_filter_keeps_non_tab_actions_untouched() -> None:
    plan = _plan_with_actions(
        SuggestedAction(
            action_type="copy_to_clipboard",
            target="hello world",
            label="Copy",
        ),
    )
    filtered = filter_unsafe_actions(plan, tab_count=0)
    assert len(filtered.suggested_actions) == 1


def test_filter_drops_open_url_with_empty_target() -> None:
    plan = _plan_with_actions(
        SuggestedAction(action_type="open_url", target="", label="Open"),
    )
    filtered = filter_unsafe_actions(plan, tab_count=0)
    assert filtered.suggested_actions == []


def test_filter_logs_rejections_with_cid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cortex.libs.logging.correlation import correlation_scope

    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=99, label="Bad"),
    )
    with caplog.at_level(logging.WARNING, logger="cortex.services.llm_engine.parser"):
        with correlation_scope("cid_test12345678"):
            filter_unsafe_actions(plan, tab_count=3)

    rejection_lines = [
        r for r in caplog.records
        if "intervention_action_rejected" in r.getMessage()
    ]
    assert rejection_lines, f"expected an action_rejected log; got {caplog.records}"
    msg = rejection_lines[0].getMessage()
    assert "tab_index_out_of_range" in msg
    assert "cid_test12345678" in msg


def test_filter_is_idempotent() -> None:
    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=0, label="Keep"),
        SuggestedAction(action_type="close_tab", tab_index=99, label="Drop"),
    )
    once = filter_unsafe_actions(plan, tab_count=5)
    twice = filter_unsafe_actions(once, tab_count=5)
    assert [a.tab_index for a in twice.suggested_actions] == [0]


# ---------------------------------------------------------------------------
# P0 §3.5 — new HYPO / RECOVERY action types
# ---------------------------------------------------------------------------


def test_resume_last_active_file_in_action_type_union() -> None:
    """Schema must accept resume_last_active_file with a file_path:line target."""
    action = SuggestedAction(
        action_type="resume_last_active_file",
        target="/home/user/auth.py:67",
        label="Resume auth.py",
    )
    assert action.action_type == "resume_last_active_file"
    assert action.target.endswith(":67")


def test_suggest_movement_break_in_action_type_union() -> None:
    action = SuggestedAction(
        action_type="suggest_movement_break",
        target="2",
        label="2-min stretch",
    )
    assert action.action_type == "suggest_movement_break"


def test_prompt_micro_commit_in_action_type_union() -> None:
    action = SuggestedAction(
        action_type="prompt_micro_commit",
        target="",
        label="Save a checkpoint",
    )
    assert action.action_type == "prompt_micro_commit"


def test_consent_levels_for_new_action_types() -> None:
    """resume_last_active_file = REVERSIBLE_ACT; others = SUGGEST (P0 §3.5)."""
    from cortex.services.consent.policy import (
        DEFAULT_ACTION_LEVELS,
        REVERSIBLE_ACT,
        SUGGEST,
    )

    assert DEFAULT_ACTION_LEVELS["resume_last_active_file"] == REVERSIBLE_ACT
    assert DEFAULT_ACTION_LEVELS["prompt_micro_commit"] == SUGGEST
    assert DEFAULT_ACTION_LEVELS["suggest_movement_break"] == SUGGEST


def test_resume_last_active_file_target_length_cap() -> None:
    """_TARGET_MAX_LEN for resume_last_active_file = 300 (see intervention.py)."""
    with pytest.raises(ValidationError) as exc:
        SuggestedAction(
            action_type="resume_last_active_file",
            target="x" * 301,
            label="too long",
        )
    assert "too long" in str(exc.value)


# ---------------------------------------------------------------------------
# An index can be "in range" and still name a tab the model never saw
# ---------------------------------------------------------------------------


def test_filter_drops_a_tab_index_that_was_never_rendered() -> None:
    """The rendered tab list is a prioritised subset carrying ORIGINAL indices.

    ``to_llm_context`` renders at most 30 tabs, chosen by priority, and keeps
    each tab's original index — so the list is frequently non-contiguous and
    its largest index can exceed the number of lines shown. The system prompt
    used to assert the opposite ("tab_index must be within range [0, N-1] where
    N = number of tabs shown"), so a compliant model renumbered a high-indexed
    tab onto a lower index belonging to a different tab, and nothing downstream
    caught it: the only check was ``tab_index >= tab_count``.
    """
    shown = frozenset({0, 3, 39})
    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=0, label="shown"),
        SuggestedAction(action_type="close_tab", tab_index=39, label="shown high"),
        # In range for a 40-tab window, but never rendered.
        SuggestedAction(action_type="close_tab", tab_index=7, label="not shown"),
    )

    filtered = filter_unsafe_actions(plan, tab_count=40, shown_tab_indices=shown)

    assert [a.tab_index for a in filtered.suggested_actions] == [0, 39]


def test_filter_without_a_shown_set_keeps_the_range_check_only() -> None:
    """Callers that cannot supply the rendered set must not lose the old guard."""
    plan = _plan_with_actions(
        SuggestedAction(action_type="close_tab", tab_index=7, label="in range"),
        SuggestedAction(action_type="close_tab", tab_index=99, label="out of range"),
    )

    filtered = filter_unsafe_actions(plan, tab_count=40)

    assert [a.tab_index for a in filtered.suggested_actions] == [7]


def test_rendered_indices_are_original_and_can_exceed_the_shown_count() -> None:
    """Pin the property the prompt rule got wrong, against the real selector."""
    from cortex.libs.schemas.context import TabInfo, llm_visible_tab_indices

    # Distinct hosts so nothing is deduplicated, and put the high-priority
    # tabs at the END of the list — which is exactly when priority ordering
    # makes the rendered indices non-contiguous.
    tabs = [
        TabInfo(
            title=f"tab {i}",
            url=f"https://host{i}.example/page",
            is_active=(i == 0),
            tab_type="goal_relevant" if i >= 25 else "distraction",
        )
        for i in range(40)
    ]

    shown = llm_visible_tab_indices(tabs)

    assert len(shown) <= 30
    assert max(shown) >= len(shown), (
        "the rendered indices are original, so the largest may exceed the "
        f"number of lines shown: max={max(shown)} count={len(shown)}"
    )
    # Non-contiguous: indices below the maximum were skipped entirely.
    assert set(range(max(shown))) - shown, "expected gaps in the rendered indices"
    # And some in-range index was never rendered, which is what the extra
    # rejection branch exists to catch.
    assert set(range(len(tabs))) - shown
