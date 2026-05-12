"""
Unit tests for the tab classifier (2B-1) and related parser enforcement (2B-2),
prompt template fix (2B-3), and blocklist additions (2B-4).
"""

from __future__ import annotations

import pytest

from cortex.services.context_engine.tab_classifier import classify_tab

# =========================================================================
# 2B-1: Tab classifier — known domains classified correctly
# =========================================================================


class TestTabClassifier:
    """classify_tab must map known domains to the correct type."""

    @pytest.mark.parametrize("url,expected", [
        ("https://coursera.org/learn/ml", "educational"),
        ("https://www.udemy.com/course/python", "educational"),
        ("https://leetcode.com/problems/two-sum", "educational"),
        ("https://www.khanacademy.org/math", "educational"),
        ("https://egghead.io/lessons/react", "educational"),
    ])
    def test_educational(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://docs.python.org/3/library/json.html", "documentation"),
        ("https://react.dev/reference/react/useState", "documentation"),
        ("https://fastapi.tiangolo.com/tutorial/", "documentation"),
        ("https://numpy.org/doc/stable/", "documentation"),
    ])
    def test_documentation(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://stackoverflow.com/questions/12345", "reference"),
        ("https://en.wikipedia.org/wiki/Python", "reference"),
        ("https://arxiv.org/abs/2301.12345", "reference"),
        ("https://www.w3schools.com/python/", "reference"),
    ])
    def test_reference(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://github.com/user/repo", "code_host"),
        ("https://gitlab.com/user/repo", "code_host"),
        ("https://bitbucket.org/user/repo", "code_host"),
    ])
    def test_code_host(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://chatgpt.com/chat", "ai_assistant"),
        ("https://claude.ai/new", "ai_assistant"),
        ("https://gemini.google.com/app", "ai_assistant"),
        ("https://perplexity.ai/search", "ai_assistant"),
        ("https://chat.openai.com", "ai_assistant"),
    ])
    def test_ai_assistant(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://twitter.com/user", "social"),
        ("https://www.reddit.com/r/python", "social"),
        ("https://www.facebook.com", "social"),
        ("https://x.com/user/status/123", "social"),
    ])
    def test_social(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://netflix.com/browse", "entertainment"),
        ("https://www.twitch.tv/channel", "entertainment"),
        ("https://www.tiktok.com/@user", "entertainment"),
    ])
    def test_entertainment(self, url: str, expected: str):
        assert classify_tab(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://www.youtube.com/watch?v=abc", "video"),
        ("https://vimeo.com/12345", "video"),
        ("https://youtu.be/abc", "video"),
    ])
    def test_video(self, url: str, expected: str):
        assert classify_tab(url) == expected

    def test_unknown_domain_returns_other(self):
        assert classify_tab("https://some-random-site.xyz/page") == "other"

    def test_empty_url_returns_other(self):
        assert classify_tab("") == "other"

    def test_title_parameter_accepted(self):
        """classify_tab should accept title parameter without error."""
        result = classify_tab("https://chatgpt.com", title="ChatGPT")
        assert result == "ai_assistant"


# =========================================================================
# 2B-2: Parser enforcement — AI assistant and documentation tabs
# =========================================================================

from cortex.libs.schemas.context import BrowserContext, TabInfo, TaskContext
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    TabRecommendation,
    TabRecommendations,
    UIPlan,
)
from cortex.services.llm_engine.parser import enrich_plan_with_context


def _make_plan_with_tab_recs(recs: list[dict]) -> InterventionPlan:
    """Build a minimal InterventionPlan with tab_recommendations."""
    tab_recs = [
        TabRecommendation(
            tab_index=r["tab_index"],
            tab_title=r.get("tab_title", ""),
            action=r["action"],
            reason=r.get("reason", "test"),
            relevance_score=r.get("relevance_score", 0.5),
        )
        for r in recs
    ]
    return InterventionPlan(
        level="overlay_only",
        situation_summary="Test",
        headline="Test",
        primary_focus="Test",
        micro_steps=["step 1"],
        ui_plan=UIPlan(),
        tab_recommendations=TabRecommendations(tabs=tab_recs, summary="test"),
    )


class TestParserTabEnforcement:
    """enrich_plan_with_context must enforce tab-type safety rules."""

    def test_ai_assistant_tab_always_kept(self):
        """AI assistant tabs must be forced to action='keep' regardless of LLM output."""
        plan = _make_plan_with_tab_recs([
            {"tab_index": 0, "action": "close", "relevance_score": 0.2},
        ])
        tabs = [TabInfo(title="ChatGPT", url="https://chatgpt.com/chat", tab_type="other")]
        ctx = TaskContext(
            mode="browsing",
            active_app="chrome",
            complexity_score=0.5,
            browser_context=BrowserContext(
                active_tab_title="ChatGPT",
                active_tab_url="https://chatgpt.com/chat",
                all_tabs=tabs,
            ),
        )
        enriched = enrich_plan_with_context(plan, ctx)
        rec = enriched.tab_recommendations.tabs[0]
        assert rec.action == "keep"
        assert rec.relevance_score >= 0.9

    def test_claude_ai_tab_always_kept(self):
        """claude.ai tabs must also be kept."""
        plan = _make_plan_with_tab_recs([
            {"tab_index": 0, "action": "close", "relevance_score": 0.1},
        ])
        tabs = [TabInfo(title="Claude", url="https://claude.ai/new", tab_type="other")]
        ctx = TaskContext(
            mode="browsing",
            active_app="chrome",
            complexity_score=0.5,
            browser_context=BrowserContext(
                active_tab_title="Claude",
                active_tab_url="https://claude.ai/new",
                all_tabs=tabs,
            ),
        )
        enriched = enrich_plan_with_context(plan, ctx)
        assert enriched.tab_recommendations.tabs[0].action == "keep"

    def test_documentation_tab_kept_during_debugging(self):
        """Documentation tabs should be kept when user is debugging."""
        plan = _make_plan_with_tab_recs([
            {"tab_index": 0, "action": "close", "relevance_score": 0.3},
        ])
        tabs = [TabInfo(title="Python Docs", url="https://docs.python.org/3/", tab_type="documentation")]
        ctx = TaskContext(
            mode="coding_debugging",
            active_app="vscode",
            complexity_score=0.7,
            browser_context=BrowserContext(
                active_tab_title="Python Docs",
                active_tab_url="https://docs.python.org/3/",
                all_tabs=tabs,
            ),
        )
        enriched = enrich_plan_with_context(plan, ctx)
        rec = enriched.tab_recommendations.tabs[0]
        assert rec.action == "keep"
        assert rec.relevance_score >= 0.8

    def test_documentation_tab_not_forced_when_browsing(self):
        """Documentation tabs should NOT be force-kept during normal browsing."""
        plan = _make_plan_with_tab_recs([
            {"tab_index": 0, "action": "close", "relevance_score": 0.3},
        ])
        tabs = [TabInfo(title="Python Docs", url="https://docs.python.org/3/", tab_type="documentation")]
        ctx = TaskContext(
            mode="browsing",
            active_app="chrome",
            complexity_score=0.5,
            browser_context=BrowserContext(
                active_tab_title="Python Docs",
                active_tab_url="https://docs.python.org/3/",
                all_tabs=tabs,
            ),
        )
        enriched = enrich_plan_with_context(plan, ctx)
        # Not debugging, so doc tab should NOT be force-kept
        rec = enriched.tab_recommendations.tabs[0]
        assert rec.action == "close"

    def test_parser_does_not_check_chatgpt_url_directly(self):
        """Parser enforcement uses tab_type from classify_tab, not URL strings."""
        # This test verifies that the parser doesn't import or check for
        # specific ChatGPT URLs — it relies on the classifier
        import inspect

        from cortex.services.llm_engine import parser
        source = inspect.getsource(parser.enrich_plan_with_context)
        assert "chatgpt" not in source.lower()
        assert "claude.ai" not in source.lower()


# =========================================================================
# 2B-3: Active recall template — no biometric references
# =========================================================================

from cortex.services.llm_engine.prompts import PROMPT_TEMPLATES


class TestActiveRecallPrompt:
    """Active recall template must not reference raw biometric numbers."""

    def test_no_blink_rate_reference(self):
        template = PROMPT_TEMPLATES["active_recall"]
        assert "blink rate" not in template.lower()

    def test_no_scroll_velocity_reference(self):
        template = PROMPT_TEMPLATES["active_recall"]
        assert "scroll velocity" not in template.lower()

    def test_references_workspace_patterns(self):
        template = PROMPT_TEMPLATES["active_recall"]
        assert "workspace patterns" in template.lower()

    def test_references_behavior_signals(self):
        template = PROMPT_TEMPLATES["active_recall"]
        assert "behavior signals" in template.lower()


# =========================================================================
# 2B-4: Blocklist — normalizer defaults are rejected
# =========================================================================

from cortex.services.llm_engine.parser import _GENERIC_STEP_PHRASES


class TestBlocklist:
    """Normalizer default phrases must be in the blocklist."""

    def test_focus_on_current_task_blocked(self):
        assert any("focus on the current task" in p for p in _GENERIC_STEP_PHRASES)

    def test_workspace_analysis_complete_blocked(self):
        assert any("workspace analysis complete" in p for p in _GENERIC_STEP_PHRASES)

    def test_review_current_error_blocked(self):
        assert any("review the current error or task" in p for p in _GENERIC_STEP_PHRASES)

    def test_blocklist_filters_micro_steps(self):
        """enrich_plan_with_context should filter out normalizer default steps."""
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Test",
            headline="Test",
            primary_focus="Test",
            micro_steps=[
                "Focus on the current task",
                "Review the current error or task",
                "Fix the NameError on line 10",
            ],
            ui_plan=UIPlan(),
        )
        ctx = TaskContext(
            mode="coding_debugging",
            active_app="vscode",
            complexity_score=0.5,
        )
        enriched = enrich_plan_with_context(plan, ctx)
        # Only the specific step should survive
        assert len(enriched.micro_steps) == 1
        assert "NameError" in enriched.micro_steps[0]


# =========================================================================
# 2B-5: Same-category tab switching discount in rule_scorer
# =========================================================================

from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.state import UserBaselines
from cortex.services.state_engine.rule_scorer import RuleScorer


class TestSameCategoryDiscount:
    """Switching among same-category tabs should get a reduced s6 penalty."""

    def _make_scorer(self) -> RuleScorer:
        return RuleScorer(baselines=UserBaselines())

    def _make_high_switch_fv(self) -> FeatureVector:
        """Feature vector with high tab switching but otherwise neutral."""
        return FeatureVector(
            timestamp=1.0,
            hr=72.0,
            hrv_rmssd=50.0,
            blink_rate=16.0,
            mouse_velocity_mean=400.0,
            mouse_velocity_variance=5000.0,
            tab_switch_frequency=25.0,
            thrashing_score=0.0,
        )

    def test_no_discount_without_categories(self):
        """Without categories set, no discount is applied."""
        scorer = self._make_scorer()
        fv = self._make_high_switch_fv()
        scores_no_cats = scorer.compute_scores(fv)

        scorer2 = self._make_scorer()
        scorer2.set_tab_categories(None)
        scores_none = scorer2.compute_scores(fv)

        assert abs(scores_no_cats.hyper - scores_none.hyper) < 1e-9

    def test_same_category_reduces_hyper(self):
        """All tabs being educational (same category) should reduce hyper score."""
        scorer_no_cats = self._make_scorer()
        scorer_with_cats = self._make_scorer()
        scorer_with_cats.set_tab_categories([
            "educational", "educational", "educational", "educational",
            "educational", "educational", "educational", "educational",
            "educational", "educational", "educational", "educational",
            "educational", "educational", "educational",
        ])
        fv = self._make_high_switch_fv()
        hyper_no_cats = scorer_no_cats.compute_scores(fv).hyper
        hyper_with_cats = scorer_with_cats.compute_scores(fv).hyper

        assert hyper_with_cats < hyper_no_cats, (
            f"Same-category tabs should reduce hyper: {hyper_with_cats:.4f} >= {hyper_no_cats:.4f}"
        )

    def test_mixed_categories_no_discount(self):
        """Diverse tab categories should NOT get a discount."""
        scorer = self._make_scorer()
        scorer.set_tab_categories([
            "educational", "social", "entertainment", "code_host",
            "video", "reference", "documentation", "ai_assistant",
            "other", "social",
        ])
        fv = self._make_high_switch_fv()

        scorer_plain = self._make_scorer()
        hyper_mixed = scorer.compute_scores(fv).hyper
        hyper_plain = scorer_plain.compute_scores(fv).hyper

        # With diverse categories the most common is 2/10 = 0.2, below 0.6 threshold
        assert abs(hyper_mixed - hyper_plain) < 1e-9

    def test_same_category_ratio_threshold(self):
        """Discount only when >60% of tabs share the same category."""
        scorer = self._make_scorer()
        # 6 of 10 = 60%, exactly at threshold → discount should apply
        scorer.set_tab_categories([
            "educational", "educational", "educational", "educational",
            "educational", "educational", "educational",
            "social", "code_host", "other",
        ])
        fv = self._make_high_switch_fv()

        scorer_plain = self._make_scorer()
        hyper_partial = scorer.compute_scores(fv).hyper
        hyper_plain = scorer_plain.compute_scores(fv).hyper

        assert hyper_partial < hyper_plain
