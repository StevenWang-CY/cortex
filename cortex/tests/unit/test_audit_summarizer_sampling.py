"""The activity recap call must not send sampling parameters.

Current Claude models reject ``temperature`` / ``top_p`` / ``top_k`` with
HTTP 400, which the planner now treats as non-retryable; the recap request
therefore carries none of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.activity import ActivitySummary
from cortex.libs.store.memory_store import InMemoryStore
from cortex.services.activity_tracker.summarizer import ActivitySummarizer


@pytest.mark.asyncio
async def test_recap_request_has_no_sampling_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Messages:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text="Recap text.")])

    monkeypatch.setattr(
        "cortex.libs.llm.anthropic_client.build_anthropic_sdk_client",
        lambda **_kw: SimpleNamespace(messages=_Messages()),
    )
    monkeypatch.setattr(
        "cortex.libs.llm.anthropic_client.resolve_anthropic_model_id",
        lambda _model, provider=None: "model-under-test",
    )
    summarizer = ActivitySummarizer(store=InMemoryStore(), llm_config=LLMConfig())
    activity = ActivitySummary(
        content_id="https://youtube.com/watch?v=abc123",
        platform="youtube",
        content_type="video",
        title="Test Video",
        url="https://youtube.com/watch?v=abc123",
        position_description="32:48 / 1:15:22",
        duration_spent_s=300,
        last_visited=1710000000000,
        completion_pct=43.0,
        topic_tags=["algorithm"],
        context_snapshot="Test content snapshot",
    )
    text = await summarizer._call_llm(activity)
    assert text == "Recap text."
    assert captured["model"] == "model-under-test"
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in captured
