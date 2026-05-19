"""Unit tests for F28: prompt-template version in LLM cache key.

The LLM cache keyed on context + state + constraints only — a template
edit served stale plans for up to ``cache_ttl_seconds``. F28 binds the
key to a stable fingerprint of ``SYSTEM_PROMPT`` and every registered
template body so the next call after a template edit misses cache.

Tests:

1. Same template version → cache hits.
2. Template body edit → fingerprint changes → cache misses.
3. SYSTEM_PROMPT edit → fingerprint changes → cache misses.
4. Fingerprint is stable across import order.
"""

from __future__ import annotations

import importlib

from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine import prompts as prompts_mod
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import build_fallback_plan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> TaskContext:
    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.6,
        editor_context=EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 40),
            symbol_at_cursor="handle_request",
            diagnostics=[],
            recent_edits=[],
        ),
    )


def _make_state() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.0, hyper=0.9, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9, overall=0.9,
        ),
        timestamp=100.0,
        dwell_seconds=35.0,
    )


# ---------------------------------------------------------------------------
# 1. Same template version → cache hit
# ---------------------------------------------------------------------------


def test_same_template_version_hits_cache() -> None:
    """A second lookup at the same template version returns the cached plan."""
    cache = LLMCache(default_ttl=300.0)
    ctx = _make_context()
    state = _make_state()
    plan = build_fallback_plan(ctx)
    cache.put(ctx, plan, state)
    cached = cache.get(ctx, state)
    assert cached is not None
    assert cached.headline == plan.headline


# ---------------------------------------------------------------------------
# 2. Template body edit → cache misses
# ---------------------------------------------------------------------------


def test_template_body_edit_invalidates_cache(
    monkeypatch: object,
) -> None:
    """Mutating a registered template body changes
    ``PROMPT_TEMPLATE_VERSION`` on the next computation, which then
    changes the cache key — so a previously cached plan is not returned."""
    cache = LLMCache(default_ttl=300.0)
    ctx = _make_context()
    state = _make_state()
    plan = build_fallback_plan(ctx)
    cache.put(ctx, plan, state)
    # Sanity: lookup hits.
    assert cache.get(ctx, state) is not None

    # Mutate a registered template body. The cache helper recomputes
    # the version on each lookup (the import-time constant is recomputed
    # by the helper here to model an edit + daemon restart).
    original = prompts_mod.PROMPT_TEMPLATES["micro_step_planner"]
    try:
        prompts_mod.PROMPT_TEMPLATES["micro_step_planner"] = (
            original + "\n\n# extra clause added by maintainer"
        )
        new_version = prompts_mod._compute_prompt_template_version()
        # Sanity check: the fingerprint moved.
        assert new_version != prompts_mod.PROMPT_TEMPLATE_VERSION
        # Now point the constant at the new value (simulates restart
        # after the template edit) and verify the lookup misses.
        monkeypatch.setattr(
            prompts_mod, "PROMPT_TEMPLATE_VERSION", new_version,
        )
        assert cache.get(ctx, state) is None
    finally:
        prompts_mod.PROMPT_TEMPLATES["micro_step_planner"] = original


# ---------------------------------------------------------------------------
# 3. SYSTEM_PROMPT edit → cache misses
# ---------------------------------------------------------------------------


def test_system_prompt_edit_invalidates_cache(
    monkeypatch: object,
) -> None:
    cache = LLMCache(default_ttl=300.0)
    ctx = _make_context()
    state = _make_state()
    plan = build_fallback_plan(ctx)
    cache.put(ctx, plan, state)
    assert cache.get(ctx, state) is not None

    original_system = prompts_mod.SYSTEM_PROMPT
    try:
        monkeypatch.setattr(
            prompts_mod,
            "SYSTEM_PROMPT",
            original_system + "\n# new policy clause",
        )
        new_version = prompts_mod._compute_prompt_template_version()
        assert new_version != prompts_mod.PROMPT_TEMPLATE_VERSION
        monkeypatch.setattr(
            prompts_mod, "PROMPT_TEMPLATE_VERSION", new_version,
        )
        assert cache.get(ctx, state) is None
    finally:
        # monkeypatch.setattr restores automatically; nothing to do.
        _ = original_system


# ---------------------------------------------------------------------------
# 4. Fingerprint is stable across import order
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_import_order() -> None:
    """Re-importing the module produces the same fingerprint.

    The hash sorts template names alphabetically before feeding the body
    in, so a future refactor (decorator-based registry, lazy template
    loaders) that changes insertion order won't shift the fingerprint
    spuriously and dump every cached plan.
    """
    v1 = prompts_mod.PROMPT_TEMPLATE_VERSION
    # Reimport — module is already in sys.modules so this just
    # re-binds; we still call _compute_prompt_template_version() with
    # the in-memory state to confirm determinism.
    importlib.reload(prompts_mod)
    v2 = prompts_mod._compute_prompt_template_version()
    assert v1 == v2
