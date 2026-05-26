"""
Tests for P0 §3.5 prompt router state-dispatch.

Contracts:

- ``select_prompt_template(context, "HYPO")`` returns
  ``"re_engage_planner"`` regardless of context mode.
- ``select_prompt_template(context, "RECOVERY")`` returns
  ``"recovery_reinforcer"``.
- Both prompt template names exist in ``PROMPT_TEMPLATES``.
- Passing ``state=None`` (or omitting it) preserves the legacy mode-only
  selection.
- ``build_user_prompt`` reads ``state.state`` and routes accordingly.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    _dispatch_by_state,
    build_user_prompt,
    select_prompt_template,
)


def _ctx(mode: str = "coding_debugging") -> TaskContext:
    return TaskContext(
        mode=mode,
        active_app="vscode",
        current_goal_hint="finish auth refactor",
        complexity_score=0.5,
    )


def _estimate(state: str) -> StateEstimate:
    return StateEstimate(
        state=state,  # type: ignore[arg-type]
        confidence=0.9,
        scores=StateScores(flow=0.1, hypo=0.1, hyper=0.1, recovery=0.1),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=120.0,
    )


# --------------------------------------------------------------------- #
# Template registry contract
# --------------------------------------------------------------------- #


def test_re_engage_planner_registered() -> None:
    assert "re_engage_planner" in PROMPT_TEMPLATES
    body = PROMPT_TEMPLATES["re_engage_planner"]
    assert "DISENGAGED" in body or "disengaged" in body
    # Required placeholders so build_user_prompt's .format() succeeds.
    for placeholder in ("{state}", "{confidence", "{dwell", "{complexity",
                         "{context}", "{goal_hint}", "{constraints_text}",
                         "{extra_context}"):
        assert placeholder in body


def test_recovery_reinforcer_registered() -> None:
    assert "recovery_reinforcer" in PROMPT_TEMPLATES
    body = PROMPT_TEMPLATES["recovery_reinforcer"]
    assert "overlay_only" in body
    assert "minimal" in body
    for placeholder in ("{state}", "{confidence", "{dwell", "{complexity",
                         "{context}", "{goal_hint}", "{constraints_text}",
                         "{extra_context}"):
        assert placeholder in body


# --------------------------------------------------------------------- #
# Dispatcher contract
# --------------------------------------------------------------------- #


def test_dispatch_returns_re_engage_for_hypo() -> None:
    assert _dispatch_by_state("HYPO") == "re_engage_planner"


def test_dispatch_returns_reinforcer_for_recovery() -> None:
    assert _dispatch_by_state("RECOVERY") == "recovery_reinforcer"


def test_dispatch_returns_none_for_hyper_and_flow() -> None:
    assert _dispatch_by_state("HYPER") is None
    assert _dispatch_by_state("FLOW") is None
    assert _dispatch_by_state("unknown") is None


# --------------------------------------------------------------------- #
# select_prompt_template contract
# --------------------------------------------------------------------- #


def test_select_prompt_template_hypo_uses_re_engage_planner() -> None:
    ctx = _ctx("coding_debugging")
    assert select_prompt_template(ctx, "HYPO") == "re_engage_planner"


def test_select_prompt_template_recovery_uses_reinforcer() -> None:
    ctx = _ctx("browsing")
    assert select_prompt_template(ctx, "RECOVERY") == "recovery_reinforcer"


def test_select_prompt_template_hyper_falls_through_to_mode() -> None:
    ctx = _ctx("coding_debugging")
    # HYPER doesn't have a state-template — falls back to mode logic.
    name = select_prompt_template(ctx, "HYPER")
    assert name in {"code_focus_reduction", "debug_error_summary"}


def test_select_prompt_template_state_none_is_backwards_compatible() -> None:
    """Passing no state must keep the legacy mode-only selection."""
    ctx = _ctx("coding_debugging")
    legacy = select_prompt_template(ctx)
    explicit = select_prompt_template(ctx, None)
    assert legacy == explicit
    # And legacy selection must not pick the new templates.
    assert legacy not in {"re_engage_planner", "recovery_reinforcer"}


# --------------------------------------------------------------------- #
# build_user_prompt integration
# --------------------------------------------------------------------- #


def test_build_user_prompt_routes_hypo_to_re_engage() -> None:
    ctx = _ctx("coding_debugging")
    est = _estimate("HYPO")
    prompt = build_user_prompt(ctx, est)
    # The HYPO template body is unique enough to fingerprint cheaply.
    assert "DISENGAGED" in prompt or "drift" in prompt.lower()


def test_build_user_prompt_routes_recovery_to_reinforcer() -> None:
    ctx = _ctx("coding_debugging")
    est = _estimate("RECOVERY")
    prompt = build_user_prompt(ctx, est)
    assert "emerged from HYPER" in prompt or "RECOVERY" in prompt


def test_build_user_prompt_keeps_hyper_on_mode_template() -> None:
    ctx = _ctx("browsing")
    est = _estimate("HYPER")
    prompt = build_user_prompt(ctx, est)
    # browsing→calm_overlay_writer; HYPO/RECOVERY strings should NOT
    # appear in the resulting prompt body.
    assert "DISENGAGED" not in prompt
    assert "emerged from HYPER" not in prompt
