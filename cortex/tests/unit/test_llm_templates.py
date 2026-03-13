"""
Unit tests for LLM prompt template registry.
"""

from __future__ import annotations

import pytest

from cortex.services.llm_engine.prompts import PROMPT_TEMPLATES


class TestPromptTemplateRegistry:
    def test_template_count(self):
        # 5 original + 5 v2.0 + 4 LeetCode = 14
        assert len(PROMPT_TEMPLATES) == 14

    def test_leetcode_templates_present(self):
        leetcode_names = [
            "restatement_scratchpad",
            "pattern_ladder",
            "amygdala_lockout",
            "parasympathetic_consolidation",
        ]
        for name in leetcode_names:
            assert name in PROMPT_TEMPLATES, f"Missing LeetCode template: {name}"
