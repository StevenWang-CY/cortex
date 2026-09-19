"""
Unit tests for LLM prompt template registry and v2 template formatting.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.context import (
    BrowserContext,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.prompts import PROMPT_TEMPLATES, build_user_prompt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    mode: str = "coding_debugging",
    with_browser: bool = False,
) -> TaskContext:
    editor = EditorContext(
        file_path="/src/main.py",
        visible_range=(1, 50),
        symbol_at_cursor="handle_request",
    )
    browser = None
    if with_browser:
        browser = BrowserContext(
            active_tab_title="Docs",
            active_tab_url="https://docs.python.org",
            all_tabs=[
                TabInfo(title="Docs", url="https://docs.python.org", tab_type="documentation"),
            ],
        )
    return TaskContext(
        mode=mode,
        active_app="vscode",
        complexity_score=0.75,
        editor_context=editor,
        browser_context=browser,
    )


def _make_state() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.9,
        scores=StateScores(flow=0.1, hypo=0.05, hyper=0.9, recovery=0.05),
        signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
        timestamp=1000.0,
        dwell_seconds=12.0,
    )


# ---------------------------------------------------------------------------
# Registry checks
# ---------------------------------------------------------------------------


class TestPromptTemplateRegistry:
    def test_template_count(self):
        # 5 original + 5 v2.0 = 10
        assert len(PROMPT_TEMPLATES) >= 10

    def test_v2_templates_present(self):
        v2_names = [
            "breathing_overlay",
            "active_recall",
            "rabbit_hole",
            "alignment_summary",
            "deep_bottleneck_diagnosis",
        ]
        for name in v2_names:
            assert name in PROMPT_TEMPLATES, f"Missing v2 template: {name}"


# ---------------------------------------------------------------------------
# v2 template formatting — ensure no KeyError on {extra_context}
# ---------------------------------------------------------------------------


class TestV2TemplateFormatting:
    """Calling build_user_prompt for each v2 template must not raise KeyError."""

    @pytest.mark.parametrize("template_name", [
        "breathing_overlay",
        "active_recall",
        "rabbit_hole",
        "alignment_summary",
    ])
    def test_v2_template_with_extra_context(self, template_name: str):
        ctx = _make_context()
        state = _make_state()
        # Should NOT raise KeyError on {extra_context}
        prompt = build_user_prompt(
            ctx, state, template_name=template_name,
            extra_context="sample extra context data",
        )
        assert "sample extra context data" in prompt

    @pytest.mark.parametrize("template_name", [
        "breathing_overlay",
        "active_recall",
        "rabbit_hole",
        "alignment_summary",
    ])
    def test_v2_template_without_extra_context_uses_default(self, template_name: str):
        ctx = _make_context()
        state = _make_state()
        # Default extra_context="" should work without KeyError
        prompt = build_user_prompt(ctx, state, template_name=template_name)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_deep_bottleneck_no_extra_context_field(self):
        """deep_bottleneck_diagnosis does NOT use {extra_context}, so should also work."""
        ctx = _make_context()
        state = _make_state()
        prompt = build_user_prompt(
            ctx, state, template_name="deep_bottleneck_diagnosis",
        )
        assert isinstance(prompt, str)
        assert "FAILING ABSTRACTION" in prompt

    def test_original_templates_still_work_with_extra_context_param(self):
        """v1 templates don't use {extra_context} but the parameter shouldn't break them."""
        ctx = _make_context()
        state = _make_state()
        for name in ["debug_error_summary", "code_focus_reduction",
                      "browser_tab_reduction", "micro_step_planner",
                      "calm_overlay_writer"]:
            prompt = build_user_prompt(
                ctx, state, template_name=name, extra_context="ignored",
            )
            assert isinstance(prompt, str)


# ---------------------------------------------------------------------------
# Token budget enforcement tests
# ---------------------------------------------------------------------------


class TestTokenBudgetEnforcement:
    """Test that build_messages enforces an 80% token budget hard cap."""

    def test_small_context_not_truncated(self):
        """Normal-sized context should not be truncated."""
        from cortex.services.llm_engine.prompts import build_messages

        ctx = _make_context()
        state = _make_state()
        msgs = build_messages(ctx, state)
        # Should have 2 messages (system + user), unmodified
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_large_context_is_truncated(self):
        """Context exceeding 80% of budget must be truncated under the limit."""
        from cortex.services.llm_engine.prompts import (
            _total_message_tokens,
            build_messages,
        )

        # Create a context with extremely large terminal output to exceed budget
        terminal = TerminalContext(
            last_n_lines=["error line " * 100] * 500,
            detected_errors=["big error " * 200] * 50,
            repeated_commands=[],
        )
        editor = EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 50),
            symbol_at_cursor="handle_request",
            visible_code="x = 1\n" * 5000,
        )
        ctx = TaskContext(
            mode="terminal_errors",
            active_app="vscode",
            complexity_score=0.75,
            editor_context=editor,
            terminal_context=terminal,
        )
        state = _make_state()

        # Use a budget that forces truncation but fits the system prompt
        max_tokens = 8000
        msgs = build_messages(ctx, state, max_context_tokens=max_tokens)

        total = _total_message_tokens(msgs)
        budget = int(max_tokens * 0.80)
        assert total <= budget, (
            f"Total tokens {total} exceeds budget {budget}"
        )

    def test_budget_respects_80_percent_cap(self):
        """Verify the 80% cap arithmetic."""
        from cortex.services.llm_engine.prompts import _enforce_token_budget, _total_message_tokens

        # Build messages that are clearly over budget
        big_content = "word " * 50000  # ~50000 chars = ~12500 tokens
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": big_content},
        ]
        max_ctx = 5000  # budget = 4000 tokens = 16000 chars
        result = _enforce_token_budget(messages, max_context_tokens=max_ctx)
        total = _total_message_tokens(result)
        assert total <= int(max_ctx * 0.80)


# ---------------------------------------------------------------------------
# Templates may only ask for what the draft schema can carry
# ---------------------------------------------------------------------------


class TestTemplatesMatchTheDraftContract:
    """A prompt that asks for an impossible field misdirects the whole plan.

    ``PlanDraft`` sets ``extra="forbid"`` and the emitted structured-output
    schema closes every object, so the model cannot return a field the draft
    does not declare — the grammar will not let it. A template that asks for
    one therefore does not fail loudly; it produces a plan shaped around an
    intervention that will never happen. ``active_recall`` asked for three
    ``recall_*`` fields and told the model the screen would blur until the
    user answered correctly, and ``ACTIVE_RECALL`` is a compatibility sink in
    the extension with no page receiver at all.
    """

    def test_no_template_asks_for_a_field_the_draft_cannot_carry(self) -> None:
        from cortex.services.llm_engine.plan_draft import PlanDraft

        declared = set(PlanDraft.model_fields)
        # Fields the model is legitimately told about live on nested draft
        # models; collect those too rather than flagging them.
        for field in PlanDraft.model_fields.values():
            annotation = field.annotation
            nested = getattr(annotation, "model_fields", None)
            if nested:
                declared |= set(nested)

        forbidden = {"recall_question", "recall_answer", "recall_context_sentence"}
        assert not (forbidden & declared), (
            "the draft now declares recall fields; update this test rather "
            "than deleting it"
        )
        for name, template in PROMPT_TEMPLATES.items():
            leaked = sorted(field for field in forbidden if field in template)
            assert not leaked, (
                f"template {name!r} asks the model for {leaked}, which the "
                "closed draft schema cannot carry — the model cannot return "
                "them and the plan is written for an intervention that never "
                "occurs"
            )

    def test_no_template_promises_a_surface_that_does_not_present(self) -> None:
        """ACTIVE_RECALL, BREATHING_OVERLAY and the lockouts are inert sinks.

        Promising the model that the screen will blur, or that the user will
        be blocked until they answer, is a promise the extension explicitly
        does not keep: those frames present nothing and mutate nothing until
        they have an exact authorization and a receipt-backed escape path.
        """
        promises = ("blur the screen", "must answer correctly to unblur")
        for name, template in PROMPT_TEMPLATES.items():
            lowered = template.lower()
            broken = [phrase for phrase in promises if phrase in lowered]
            assert not broken, (
                f"template {name!r} promises {broken}; no surface delivers it"
            )
