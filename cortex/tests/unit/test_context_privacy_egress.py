"""Workspace content must not reach a provider outside the privacy boundary.

``ContextBroker`` / ``PrivacyAwarePlanner`` is the documented egress boundary:
it classifies every context field, sanitises free text, requires an
acknowledged disclosure, and previews the request. ``ActivitySummarizer``
deliberately does not go through the planner — it wants raw text back rather
than a typed ``InterventionPlan`` — and so inherited none of that.

It interpolated the raw window title, the raw position description and a
200-character workspace ``context_snapshot`` straight into a prompt and sent
it to the provider. ``LLMPrivacyConfig.external_transport_enabled`` is False
by default and only becomes true under ``planner_mode == "external_redacted"``
with ``external_context_enabled`` and a matching consent revision, so the old
path egressed workspace content from installs that had never enabled external
context at all.

These tests pin both halves: the consent gate, and the sanitisation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.activity import ActivitySummary
from cortex.libs.store.memory_store import InMemoryStore
from cortex.services.activity_tracker.summarizer import ActivitySummarizer

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The only two modules allowed to construct a provider SDK client. Everything
# else must reach the provider through the broker-wrapped planner.
_ALLOWED_SDK_CALLERS = {
    "cortex/libs/llm/anthropic_client.py",
    "cortex/services/llm_engine/anthropic_planner.py",
    "cortex/services/activity_tracker/summarizer.py",
}


def _consenting_config() -> LLMConfig:
    return LLMConfig(
        privacy={
            "planner_mode": "external_redacted",
            "external_context_enabled": True,
            "consent_revision": "context-disclosure-v1",
        },
    )


def _activity(**overrides: Any) -> ActivitySummary:
    payload: dict[str, Any] = {
        "content_id": "https://example.com/watch?v=abc123",
        "platform": "youtube",
        "content_type": "video",
        "title": "Test Video",
        "url": "https://example.com/watch?v=abc123",
        "position_description": "32:48 / 1:15:22",
        "duration_spent_s": 300,
        "last_visited": 1_710_000_000_000,
        "completion_pct": 43.0,
        "topic_tags": ["algorithm"],
        "context_snapshot": "Test content snapshot",
    }
    payload.update(overrides)
    return ActivitySummary(**payload)


def _capturing_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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
    return captured


@pytest.mark.asyncio
async def test_recap_refuses_to_egress_without_the_acknowledged_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default install has not enabled external context — nothing may leave."""
    captured = _capturing_sdk(monkeypatch)
    summarizer = ActivitySummarizer(store=InMemoryStore(), llm_config=LLMConfig())

    with pytest.raises(ValueError, match="external_context_disabled"):
        await summarizer._call_llm(_activity())

    assert captured == {}, "no provider request may be built before the gate"


@pytest.mark.asyncio
async def test_recap_falls_back_to_the_local_template_when_egress_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must degrade to the offline recap, not surface an error."""
    _capturing_sdk(monkeypatch)
    summarizer = ActivitySummarizer(store=InMemoryStore(), llm_config=LLMConfig())

    recap = await summarizer._generate_recap(_activity())

    assert recap
    assert "Recap text." not in recap


@pytest.mark.asyncio
async def test_recap_sanitises_workspace_content_before_it_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secrets and absolute paths in the snapshot must not reach the provider."""
    captured = _capturing_sdk(monkeypatch)
    summarizer = ActivitySummarizer(
        store=InMemoryStore(), llm_config=_consenting_config(),
    )

    await summarizer._call_llm(_activity(
        title="notes /Users/realperson/Desktop/secret-project/plan.md",
        context_snapshot=(
            "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY "
            "and see /Users/realperson/private/keys.txt"
        ),
    ))

    sent = str(captured.get("messages"))
    # The home directory layout is the user's, not the model's business.
    assert "/Users/realperson" not in sent
    assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" not in sent
    assert "REDACTED" in sent


def test_provider_sdk_is_constructed_only_inside_the_privacy_boundary() -> None:
    """An architectural fitness check on the egress chokepoint.

    A third module calling ``build_anthropic_sdk_client`` would be a new,
    unreviewed egress path. Adding one is allowed — but it has to be a
    deliberate edit to this list, with the privacy gate and sanitisation that
    ``summarizer._call_llm`` now carries.
    """
    offenders: list[str] = []
    for path in (_REPO_ROOT / "cortex").rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative.startswith("cortex/tests/") or relative in _ALLOWED_SDK_CALLERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                if name == "build_anthropic_sdk_client":
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], (
        "provider SDK constructed outside the privacy boundary: "
        + ", ".join(offenders)
    )
