"""
Tests for Phase 10: LLM Engine

Covers:
- JSON parsing (valid, malformed, invalid)
- Pydantic validation & normalization
- Prompt template selection by mode
- Prompt building with context/state/constraints
- LLM cache (hit, miss, expiration, eviction, stats)
- Fallback plan generation
- RemoteQwenClient (mocked HTTP)
- LocalOllamaClient (mocked HTTP)
- Module imports
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints, UIPlan
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import LLMError, RuleBasedLLMClient, build_fallback_plan
from cortex.services.llm_engine.azure_openai import AzureOpenAIClient
from cortex.services.llm_engine.local_ollama import LocalOllamaClient
from cortex.services.llm_engine.parser import (
    parse_and_validate,
    parse_llm_response,
    validate_intervention_plan,
)
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    SYSTEM_PROMPT,
    build_messages,
    build_user_prompt,
    select_prompt_template,
)
from cortex.services.llm_engine.remote_qwen import RemoteQwenClient

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_context(
    mode: str = "coding_debugging",
    active_app: str = "vscode",
    complexity: float = 0.75,
    with_editor: bool = True,
    with_terminal: bool = True,
    with_browser: bool = False,
    tab_count: int = 3,
) -> TaskContext:
    editor = None
    if with_editor:
        editor = EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 50),
            symbol_at_cursor="handle_request",
            diagnostics=[
                Diagnostic(severity="error", message="NameError: x is not defined", line=10),
                Diagnostic(severity="warning", message="Unused import os", line=1),
            ],
            visible_code="def handle_request():\n    return x  # NameError",
        )

    terminal = None
    if with_terminal:
        terminal = TerminalContext(
            last_n_lines=["$ python main.py", "NameError: x is not defined"],
            detected_errors=["NameError: x is not defined"],
            repeated_commands=["python main.py"],
        )

    browser = None
    if with_browser:
        tabs = [
            TabInfo(title=f"Tab {i}", url=f"https://example.com/{i}", tab_type="other")
            for i in range(tab_count)
        ]
        browser = BrowserContext(
            active_tab_title="Stack Overflow",
            active_tab_url="https://stackoverflow.com/q/123",
            all_tabs=tabs,
        )

    return TaskContext(
        mode=mode,
        active_app=active_app,
        complexity_score=complexity,
        editor_context=editor,
        terminal_context=terminal,
        browser_context=browser,
    )


def _make_state(
    state: str = "HYPER",
    confidence: float = 0.9,
    dwell: float = 12.0,
) -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=confidence,
        scores=StateScores(flow=0.1, hypo=0.05, hyper=0.9, recovery=0.05),
        signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
        timestamp=1000.0,
        dwell_seconds=dwell,
    )


VALID_PLAN_JSON = json.dumps({
    "level": "simplified_workspace",
    "situation_summary": "NameError on line 10.",
    "headline": "Fix the undefined variable",
    "primary_focus": "Define x before using it in handle_request",
    "micro_steps": ["Add x = 0 before line 10", "Re-run python main.py"],
    "hide_targets": ["editor_symbols_except_current_function"],
    "ui_plan": {
        "dim_background": True,
        "show_overlay": True,
        "fold_unrelated_code": True,
        "intervention_type": "simplified_workspace",
    },
    "tone": "direct",
})


# ===========================================================================
# JSON Parsing Tests
# ===========================================================================


class TestParseValidJSON(unittest.TestCase):
    """Test parsing of well-formed JSON."""

    def test_valid_plan(self):
        result = parse_llm_response(VALID_PLAN_JSON)
        assert result is not None
        assert result["level"] == "simplified_workspace"
        assert len(result["micro_steps"]) == 2

    def test_minimal_json_object(self):
        result = parse_llm_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_nested_json(self):
        raw = '{"a": {"b": [1, 2, 3]}, "c": true}'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["a"]["b"] == [1, 2, 3]


class TestParseMalformedJSON(unittest.TestCase):
    """Test fault-tolerant parsing of malformed JSON."""

    def test_missing_closing_brace(self):
        raw = '{"level": "overlay_only", "headline": "Test", "micro_steps": ["step 1"]'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["headline"] == "Test"

    def test_trailing_comma_object(self):
        raw = '{"level": "overlay_only", "headline": "Test",}'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["level"] == "overlay_only"

    def test_trailing_comma_array(self):
        raw = '{"steps": ["a", "b",]}'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["steps"] == ["a", "b"]

    def test_markdown_wrapped(self):
        raw = '```json\n{"level": "overlay_only", "headline": "Test", "micro_steps": ["step"]}\n```'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["level"] == "overlay_only"

    def test_preamble_text(self):
        raw = 'Here is the intervention plan:\n{"level": "overlay_only", "headline": "Test", "micro_steps": ["step"]}'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["level"] == "overlay_only"

    def test_empty_response(self):
        assert parse_llm_response("") is None
        assert parse_llm_response("   ") is None

    def test_not_json(self):
        assert parse_llm_response("I think you should take a break.") is None

    def test_combined_issues(self):
        """Markdown + trailing comma + missing brace."""
        raw = '```json\n{"level": "overlay_only", "data": [1, 2,]\n```'
        result = parse_llm_response(raw)
        assert result is not None
        assert result["level"] == "overlay_only"


# ===========================================================================
# Validation Tests
# ===========================================================================


class TestValidateInterventionPlan(unittest.TestCase):
    """Test Pydantic validation of parsed JSON."""

    def test_valid_data(self):
        data = json.loads(VALID_PLAN_JSON)
        plan = validate_intervention_plan(data)
        assert plan is not None
        assert isinstance(plan, InterventionPlan)
        assert plan.level == "simplified_workspace"
        assert plan.is_valid

    def test_normalization_adds_defaults(self):
        """Missing optional fields should get defaults."""
        data = {"level": "overlay_only"}
        plan = validate_intervention_plan(data)
        assert plan is not None
        assert plan.headline == "Focus on the current task"
        assert len(plan.micro_steps) >= 1

    def test_normalization_clamps_steps(self):
        """More than 3 steps should be clamped to 3."""
        data = {
            "level": "overlay_only",
            "situation_summary": "Test",
            "headline": "Test",
            "primary_focus": "Test",
            "micro_steps": ["a", "b", "c", "d", "e"],
        }
        plan = validate_intervention_plan(data)
        assert plan is not None
        assert len(plan.micro_steps) == 3

    def test_infers_level_from_ui_plan(self):
        data = {
            "situation_summary": "Test",
            "headline": "Test",
            "primary_focus": "Test",
            "micro_steps": ["step"],
            "ui_plan": {"intervention_type": "guided_mode"},
        }
        plan = validate_intervention_plan(data)
        assert plan is not None
        assert plan.level == "guided_mode"

    def test_wrong_schema_returns_none(self):
        """Data with no usable fields should still get normalized and succeed."""
        data = {"action": "simplify", "message": "hi"}
        plan = validate_intervention_plan(data)
        # Normalization adds defaults, so this should succeed
        assert plan is not None

    def test_parse_and_validate_end_to_end(self):
        plan = parse_and_validate(VALID_PLAN_JSON)
        assert plan is not None
        assert plan.level == "simplified_workspace"

    def test_parse_and_validate_malformed(self):
        raw = '{"level": "overlay_only", "headline": "Test"'  # missing brace
        plan = parse_and_validate(raw)
        assert plan is not None
        assert plan.level == "overlay_only"


# ===========================================================================
# Prompt Template Tests
# ===========================================================================


class TestPromptSelection(unittest.TestCase):
    """Test prompt template selection by workspace mode."""

    def test_terminal_errors_selects_debug(self):
        ctx = _make_context(mode="terminal_errors")
        assert select_prompt_template(ctx) == "debug_error_summary"

    def test_coding_with_errors_selects_debug(self):
        ctx = _make_context(mode="coding_debugging", with_terminal=True)
        assert select_prompt_template(ctx) == "debug_error_summary"

    def test_coding_no_errors_selects_code_focus(self):
        ctx = _make_context(mode="coding_debugging", with_editor=False, with_terminal=False)
        assert select_prompt_template(ctx) == "code_focus_reduction"

    def test_browsing_many_tabs(self):
        ctx = _make_context(mode="browsing", with_editor=False, with_terminal=False, with_browser=True, tab_count=10)
        assert select_prompt_template(ctx) == "browser_tab_reduction"

    def test_browsing_few_tabs(self):
        ctx = _make_context(mode="browsing", with_editor=False, with_terminal=False, with_browser=True, tab_count=2)
        assert select_prompt_template(ctx) == "calm_overlay_writer"

    def test_reading_docs(self):
        ctx = _make_context(mode="reading_docs", with_editor=False, with_terminal=False)
        assert select_prompt_template(ctx) == "calm_overlay_writer"

    def test_mixed_selects_micro_step(self):
        ctx = _make_context(mode="mixed", with_editor=False, with_terminal=False)
        assert select_prompt_template(ctx) == "micro_step_planner"

    def test_all_templates_exist(self):
        assert len(PROMPT_TEMPLATES) == 11
        for name in ["debug_error_summary", "code_focus_reduction", "browser_tab_reduction",
                      "micro_step_planner", "calm_overlay_writer",
                      "breathing_overlay", "active_recall", "rabbit_hole",
                      "alignment_summary", "deep_bottleneck_diagnosis"]:
            assert name in PROMPT_TEMPLATES


class TestPromptBuilding(unittest.TestCase):
    """Test prompt construction."""

    def test_build_user_prompt_includes_context(self):
        ctx = _make_context()
        state = _make_state()
        prompt = build_user_prompt(ctx, state)
        assert "main.py" in prompt
        assert "HYPER" in prompt
        assert "90%" in prompt

    def test_build_user_prompt_with_constraints(self):
        ctx = _make_context()
        state = _make_state()
        constraints = SimplificationConstraints(max_visible_tabs=2, max_visible_lines=30)
        prompt = build_user_prompt(ctx, state, constraints)
        assert "Max visible tabs: 2" in prompt
        assert "Max visible lines: 30" in prompt

    def test_build_messages_structure(self):
        ctx = _make_context()
        state = _make_state()
        msgs = build_messages(ctx, state)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "Cortex" in msgs[0]["content"]

    def test_override_template_name(self):
        ctx = _make_context(mode="coding_debugging")
        state = _make_state()
        prompt = build_user_prompt(ctx, state, template_name="calm_overlay_writer")
        assert "empathetic" in prompt.lower() or "overwhelmed" in prompt.lower()

    def test_system_prompt_has_schema(self):
        assert "situation_summary" in SYSTEM_PROMPT
        assert "micro_steps" in SYSTEM_PROMPT
        assert "hide_targets" in SYSTEM_PROMPT


# ===========================================================================
# Cache Tests
# ===========================================================================


class TestLLMCache(unittest.TestCase):
    """Test LRU cache behavior."""

    def setUp(self):
        self.cache = LLMCache(max_size=3, default_ttl=60.0)
        self.ctx = _make_context()
        self.plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Test",
            headline="Test headline",
            primary_focus="Test focus",
            micro_steps=["step 1"],
            ui_plan=UIPlan(),
        )

    def test_miss_then_hit(self):
        assert self.cache.get(self.ctx, now=100.0) is None
        self.cache.put(self.ctx, self.plan, now=100.0)
        result = self.cache.get(self.ctx, now=101.0)
        assert result is not None
        assert result.headline == "Test headline"

    def test_expiration(self):
        self.cache.put(self.ctx, self.plan, now=100.0, ttl=10.0)
        assert self.cache.get(self.ctx, now=105.0) is not None  # within TTL
        assert self.cache.get(self.ctx, now=115.0) is None  # expired

    def test_eviction_at_capacity(self):
        for i in range(4):
            ctx = _make_context(complexity=0.1 * (i + 1))
            self.cache.put(ctx, self.plan, now=100.0)
        # First entry should be evicted (max_size=3)
        assert self.cache.size == 3

    def test_invalidate(self):
        self.cache.put(self.ctx, self.plan, now=100.0)
        assert self.cache.invalidate(self.ctx) is True
        assert self.cache.get(self.ctx, now=101.0) is None
        assert self.cache.invalidate(self.ctx) is False  # already gone

    def test_clear(self):
        self.cache.put(self.ctx, self.plan, now=100.0)
        self.cache.clear()
        assert self.cache.size == 0

    def test_hit_rate(self):
        assert self.cache.hit_rate == 0.0
        self.cache.put(self.ctx, self.plan, now=100.0)
        self.cache.get(self.ctx, now=101.0)  # hit
        self.cache.get(_make_context(complexity=0.1), now=101.0)  # miss
        assert self.cache.hit_rate == 0.5

    def test_stats(self):
        stats = self.cache.stats
        assert "size" in stats
        assert "max_size" in stats
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats

    def test_prune_expired(self):
        self.cache.put(self.ctx, self.plan, now=100.0, ttl=5.0)
        ctx2 = _make_context(complexity=0.5)
        self.cache.put(ctx2, self.plan, now=100.0, ttl=100.0)
        removed = self.cache.prune_expired(now=110.0)
        assert removed == 1
        assert self.cache.size == 1

    def test_state_change_busts_cache(self):
        state1 = _make_state(confidence=0.82)
        state2 = _make_state(confidence=0.97)
        self.cache.put(self.ctx, self.plan, state1, now=100.0)
        assert self.cache.get(self.ctx, state1, now=101.0) is not None
        assert self.cache.get(self.ctx, state2, now=101.0) is None

    def test_constraints_change_busts_cache(self):
        constraints1 = SimplificationConstraints(max_visible_tabs=3)
        constraints2 = SimplificationConstraints(max_visible_tabs=1)
        self.cache.put(self.ctx, self.plan, None, constraints1, now=100.0)
        assert self.cache.get(self.ctx, None, constraints1, now=101.0) is not None
        assert self.cache.get(self.ctx, None, constraints2, now=101.0) is None


# ===========================================================================
# Fallback Plan Tests
# ===========================================================================


class TestFallbackPlan(unittest.TestCase):
    """Test rule-based fallback plan generation."""

    def test_fallback_without_context(self):
        plan = build_fallback_plan()
        assert isinstance(plan, InterventionPlan)
        assert plan.level == "overlay_only"
        assert plan.is_valid
        assert not plan.is_destructive

    def test_fallback_with_terminal_errors(self):
        ctx = _make_context(with_terminal=True)
        plan = build_fallback_plan(ctx)
        assert "error" in plan.situation_summary.lower() or "Error" in plan.situation_summary

    def test_fallback_with_editor_errors(self):
        ctx = _make_context(with_terminal=False, with_editor=True)
        plan = build_fallback_plan(ctx)
        assert "error" in plan.situation_summary.lower()

    def test_fallback_with_no_errors(self):
        ctx = _make_context(with_terminal=False, with_editor=False)
        plan = build_fallback_plan(ctx)
        assert plan.headline == "Focus on the function you're in"


# ===========================================================================
# RemoteQwenClient Tests (mocked HTTP)
# ===========================================================================


def _make_remote_client():
    config = LLMConfig(mode="remote", timeout_seconds=5.0)
    return RemoteQwenClient(config=config)


def _mock_response(status_code: int, json_data: dict) -> httpx.Response:
    """Create a mock httpx.Response with a request attached (needed for raise_for_status)."""
    request = httpx.Request("POST", "http://test")
    return httpx.Response(status_code, json=json_data, request=request)


@pytest.mark.asyncio
async def test_remote_generate_plan_success():
    """Successful API call returns parsed plan."""
    client = _make_remote_client()
    ctx = _make_context()
    state = _make_state()
    api_response = {
        "choices": [{"message": {"content": VALID_PLAN_JSON}}]
    }
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        plan = await client.generate_intervention_plan(ctx, state)
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "simplified_workspace"


@pytest.mark.asyncio
async def test_remote_generate_plan_fallback_on_http_error():
    """HTTP errors should trigger fallback plan after retries."""
    client = _make_remote_client()
    ctx = _make_context()
    state = _make_state()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        plan = await client.generate_intervention_plan(ctx, state)
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "overlay_only"  # fallback


@pytest.mark.asyncio
async def test_remote_generate_plan_uses_cache():
    """Second call should use cached result."""
    client = _make_remote_client()
    ctx = _make_context()
    state = _make_state()
    api_response = {
        "choices": [{"message": {"content": VALID_PLAN_JSON}}]
    }
    mock_resp = _mock_response(200, api_response)
    mock_post = AsyncMock(return_value=mock_resp)
    with patch("httpx.AsyncClient.post", mock_post):
        plan1 = await client.generate_intervention_plan(ctx, state)
        plan2 = await client.generate_intervention_plan(ctx, state)
    assert plan1.level == plan2.level
    # post should only be called once (second call uses cache)
    assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_remote_generate_plan_retries_when_state_changes():
    client = _make_remote_client()
    ctx = _make_context()
    state1 = _make_state(confidence=0.82)
    state2 = _make_state(confidence=0.97)
    api_response = {
        "choices": [{"message": {"content": VALID_PLAN_JSON}}]
    }
    mock_resp = _mock_response(200, api_response)
    mock_post = AsyncMock(return_value=mock_resp)
    with patch("httpx.AsyncClient.post", mock_post):
        await client.generate_intervention_plan(ctx, state1)
        await client.generate_intervention_plan(ctx, state2)
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_remote_generate_plan_opens_tunnel_automatically():
    config = LLMConfig(mode="remote", timeout_seconds=5.0)
    client = RemoteQwenClient(config=config)
    ctx = _make_context()
    state = _make_state()
    with patch.object(client, "open_tunnel", AsyncMock(return_value=True)) as open_tunnel:
        with patch.object(client, "_call_api", AsyncMock(return_value=VALID_PLAN_JSON)):
            plan = await client.generate_intervention_plan(ctx, state)
    assert plan.level == "simplified_workspace"
    open_tunnel.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_generate_plan_handles_content_blocks():
    config = LLMConfig(mode="remote", timeout_seconds=5.0)
    config.remote.ssh_tunnel = False
    client = RemoteQwenClient(config=config)
    ctx = _make_context()
    state = _make_state()
    api_response = {
        "choices": [{
            "message": {
                "content": [{"type": "text", "text": VALID_PLAN_JSON}],
            }
        }]
    }
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        plan = await client.generate_intervention_plan(ctx, state)
    assert plan.level == "simplified_workspace"


def test_remote_client_uses_remote_host_without_tunnel():
    config = LLMConfig(mode="remote")
    config.remote.host = "llm.example.org"
    config.remote.port = 9911
    config.remote.ssh_tunnel = False
    client = RemoteQwenClient(config=config)
    assert client._api_base_url == "http://llm.example.org:9911"


@pytest.mark.asyncio
async def test_remote_health_check_success():
    client = _make_remote_client()
    mock_response = httpx.Response(200, json={"data": []})
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        assert await client.health_check() is True


@pytest.mark.asyncio
async def test_remote_health_check_failure():
    client = _make_remote_client()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        assert await client.health_check() is False


# ===========================================================================
# LocalOllamaClient Tests (mocked HTTP)
# ===========================================================================


def _make_ollama_client():
    config = LLMConfig(mode="local", timeout_seconds=5.0)
    return LocalOllamaClient(config=config)


def _make_azure_client():
    config = LLMConfig(mode="azure", timeout_seconds=5.0)
    config.azure.endpoint = "https://example-resource.openai.azure.com/"
    config.azure.api_key = "test-key"
    config.azure.deployment_name = "gpt-5-mini"
    config.azure.reasoning_deployment_name = "gpt-5-mini-reasoning"
    config.azure.api_version = "2025-01-01-preview"
    config.fallback_mode = "local_ollama"
    return AzureOpenAIClient(config=config)


@pytest.mark.asyncio
async def test_ollama_generate_plan_success():
    client = _make_ollama_client()
    ctx = _make_context()
    state = _make_state()
    api_response = {"message": {"content": VALID_PLAN_JSON}}
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        plan = await client.generate_intervention_plan(ctx, state)
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "simplified_workspace"


@pytest.mark.asyncio
async def test_ollama_generate_plan_fallback_on_error():
    client = _make_ollama_client()
    ctx = _make_context()
    state = _make_state()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        plan = await client.generate_intervention_plan(ctx, state)
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "overlay_only"  # fallback


@pytest.mark.asyncio
async def test_ollama_health_check_success():
    client = _make_ollama_client()
    mock_response = httpx.Response(200, json={"models": []})
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        assert await client.health_check() is True


@pytest.mark.asyncio
async def test_ollama_health_check_failure():
    client = _make_ollama_client()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        assert await client.health_check() is False


# ===========================================================================
# AzureOpenAIClient Tests (mocked HTTP)
# ===========================================================================


@pytest.mark.asyncio
async def test_azure_generate_plan_success():
    client = _make_azure_client()
    ctx = _make_context()
    state = _make_state()
    api_response = {"choices": [{"message": {"content": VALID_PLAN_JSON}}]}
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as post_mock:
        plan = await client.generate_intervention_plan(ctx, state)
    assert isinstance(plan, InterventionPlan)
    assert plan.level == "simplified_workspace"
    sent_payload = post_mock.await_args.kwargs["json"]
    assert "max_completion_tokens" in sent_payload
    assert "max_tokens" not in sent_payload


@pytest.mark.asyncio
async def test_azure_uses_reasoning_deployment_for_hyper_state():
    client = _make_azure_client()
    ctx = _make_context()
    state = _make_state(state="HYPER", confidence=0.97)
    api_response = {"choices": [{"message": {"content": VALID_PLAN_JSON}}]}
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as post_mock:
        await client.generate_intervention_plan(ctx, state)
    called_url = post_mock.await_args.args[0]
    assert "gpt-5-mini-reasoning" in called_url


@pytest.mark.asyncio
async def test_azure_falls_back_to_ollama_then_rule_based():
    client = _make_azure_client()
    ctx = _make_context()
    state = _make_state()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        with patch.object(client._ollama, "generate_intervention_plan", AsyncMock(return_value=build_fallback_plan(ctx))) as ollama_mock:
            plan = await client.generate_intervention_plan(ctx, state)
    assert plan.level == "overlay_only"
    ollama_mock.assert_awaited()


@pytest.mark.asyncio
async def test_azure_health_check_success():
    client = _make_azure_client()
    mock_response = httpx.Response(200, json={"data": []})
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        assert await client.health_check() is True


@pytest.mark.asyncio
async def test_azure_payload_includes_json_response_format():
    """Azure API payload must include response_format for structured JSON output."""
    client = _make_azure_client()
    ctx = _make_context()
    state = _make_state()
    api_response = {"choices": [{"message": {"content": VALID_PLAN_JSON}}]}
    mock_resp = _mock_response(200, api_response)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as post_mock:
        await client.generate_intervention_plan(ctx, state)
    sent_payload = post_mock.await_args.kwargs["json"]
    assert "response_format" in sent_payload
    assert sent_payload["response_format"] == {"type": "json_object"}


def test_azure_uses_keychain_when_api_key_missing():
    config = LLMConfig(mode="azure")
    config.azure.endpoint = "https://example-resource.openai.azure.com/"
    config.azure.deployment_name = "gpt-5-mini"
    with patch("cortex.services.llm_engine.azure_openai.get_keychain_password", return_value="from-keychain"):
        client = AzureOpenAIClient(config=config)
        assert client._api_key == "from-keychain"


# ===========================================================================
# LLMError Tests
# ===========================================================================


class TestLLMError(unittest.TestCase):
    """Test LLMError exception."""

    def test_basic_error(self):
        err = LLMError("something went wrong")
        assert str(err) == "something went wrong"
        assert err.retries_exhausted is False

    def test_retries_exhausted(self):
        err = LLMError("failed", retries_exhausted=True)
        assert err.retries_exhausted is True


# ===========================================================================
# Import Tests
# ===========================================================================


class TestImports(unittest.TestCase):
    """Verify all public exports are importable."""

    def test_import_client(self):
        from cortex.services.llm_engine import LLMClient, LLMError, RuleBasedLLMClient, build_fallback_plan

        assert LLMClient is not None
        assert LLMError is not None
        assert RuleBasedLLMClient is not None
        assert build_fallback_plan is not None

    def test_import_prompts(self):
        from cortex.services.llm_engine import (
            PROMPT_TEMPLATES,
            SYSTEM_PROMPT,
            build_messages,
            build_user_prompt,
            select_prompt_template,
        )

        assert len(PROMPT_TEMPLATES) == 11
        assert SYSTEM_PROMPT is not None
        assert callable(build_messages)
        assert callable(build_user_prompt)
        assert callable(select_prompt_template)

    def test_import_parser(self):
        from cortex.services.llm_engine import (
            parse_and_validate,
            parse_llm_response,
            validate_intervention_plan,
        )

        assert callable(parse_and_validate)
        assert callable(parse_llm_response)
        assert callable(validate_intervention_plan)

    def test_import_cache(self):
        from cortex.services.llm_engine import LLMCache
        assert LLMCache is not None

    def test_import_clients(self):
        from cortex.services.llm_engine import AzureOpenAIClient, LocalOllamaClient, RemoteQwenClient

        assert RemoteQwenClient is not None
        assert LocalOllamaClient is not None
        assert AzureOpenAIClient is not None


if __name__ == "__main__":
    unittest.main()
