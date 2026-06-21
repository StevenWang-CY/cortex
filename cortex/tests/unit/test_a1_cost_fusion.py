"""A1 remediation: COST-BUDGET-ZERO, CONTRACT-2 cost probes, PIPE-CLOCK staleness.

These cover three Phase-4 remediation items whose owner agent died mid-run; the
lead implemented the fixes and these tests fail before / pass after.
"""

from __future__ import annotations

from pathlib import Path

from cortex.libs.schemas.features import (
    KinematicFeatures,
    PhysioFeatures,
)
from cortex.services.llm_engine.cost_tracker import (
    CostTracker,
    probe_active_model,
    probe_token_totals,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion

# ─── COST-BUDGET-ZERO ────────────────────────────────────────────────────
# daily_cost_budget_usd == 0 is the documented "unlimited" sentinel. It must
# construct (not raise — the planner swallowed the ValueError and dropped the
# tracker entirely), keep recording spend, and never escalate to KILL.


def test_cost_tracker_kill_zero_constructs(tmp_path: Path) -> None:
    # Pre-fix this raised ValueError("warn_usd and kill_usd must be positive").
    tracker = CostTracker(tmp_path / "ledger.json", warn_usd=5.0, kill_usd=0.0)
    assert tracker.kill_usd == 0.0


def test_cost_tracker_unlimited_records_but_never_kills(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path / "ledger.json", warn_usd=5.0, kill_usd=0.0)
    tracker.record("cid-1", "claude-test", 100.0)
    # Spend is still tracked...
    assert tracker.today_total_usd() == 100.0
    # ...but an unlimited (0) kill cap never exhausts / kills.
    assert tracker.budget_exhausted() is False
    assert tracker.check_budget() != "KILL"


def test_cost_tracker_zero_warn_and_kill_is_ok(tmp_path: Path) -> None:
    # Both disabled => pure accounting, always OK.
    tracker = CostTracker(tmp_path / "ledger.json", warn_usd=0.0, kill_usd=0.0)
    tracker.record("cid-1", "claude-test", 999.0)
    assert tracker.check_budget() == "OK"
    assert tracker.today_total_usd() == 999.0


def test_cost_tracker_positive_caps_still_kill(tmp_path: Path) -> None:
    # Regression guard: a real positive cap must still fire KILL.
    tracker = CostTracker(tmp_path / "ledger.json", warn_usd=1.0, kill_usd=2.0)
    tracker.record("cid-1", "claude-test", 5.0)
    assert tracker.check_budget() == "KILL"
    assert tracker.budget_exhausted() is True


def test_cost_tracker_rejects_kill_below_warn_when_both_set(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        CostTracker(tmp_path / "ledger.json", warn_usd=10.0, kill_usd=2.0)


# ─── CONTRACT-2: shared cost-response probes ─────────────────────────────


def test_probe_token_totals_none_when_absent() -> None:
    class _NoTokens:
        pass

    assert probe_token_totals(_NoTokens()) == (None, None)
    assert probe_token_totals(None) == (None, None)


def test_probe_token_totals_reads_attr_and_callable() -> None:
    class _Tracker:
        prompt_tokens_today = 1200

        def completion_tokens_today(self) -> int:
            return 340

    assert probe_token_totals(_Tracker()) == (1200, 340)


def test_probe_token_totals_rejects_bool() -> None:
    class _Bad:
        prompt_tokens_today = True  # bool is an int subclass — must be ignored
        completion_tokens_today = 7

    assert probe_token_totals(_Bad()) == (None, 7)


def test_probe_active_model_attr_then_config() -> None:
    class _Cfg:
        model = "claude-from-config"

    class _Direct:
        model = "claude-direct"

    class _ViaConfig:
        config = _Cfg()

    assert probe_active_model(_Direct()) == "claude-direct"
    assert probe_active_model(_ViaConfig()) == "claude-from-config"
    assert probe_active_model(None) is None
    assert probe_active_model(object()) is None


# ─── PIPE-CLOCK: fusion staleness penalty ────────────────────────────────
# The penalty multiplies per-channel quality when now - stamp > 3s. The bug
# was the daemon stamping with a time.time() epoch while fuse() uses
# time.monotonic(), so staleness was hugely negative and the penalty was dead.
# With consistent (monotonic) stamps the penalty must engage.


def _physio() -> PhysioFeatures:
    return PhysioFeatures(pulse_quality=0.9, valid=True)


def _kin() -> KinematicFeatures:
    return KinematicFeatures(confidence=0.8)


def test_fresh_quality_is_full_strength() -> None:
    fusion = FeatureFusion()
    fusion.update_physio(_physio(), timestamp=100.0)
    fusion.update_kinematics(_kin(), timestamp=100.0)
    _, sq = fusion.fuse(timestamp=100.0)
    assert sq.physio == 0.9
    assert sq.kinematics == 0.8


def test_stale_physio_and_kinematics_decay() -> None:
    fusion = FeatureFusion()
    fusion.update_physio(_physio(), timestamp=100.0)
    fusion.update_kinematics(_kin(), timestamp=100.0)
    # 4s later with no new updates: staleness 4 > 3 -> penalty *0.9.
    _, sq = fusion.fuse(timestamp=104.0)
    assert sq.physio < 0.9
    assert sq.kinematics < 0.8
    # And keeps decaying further out.
    _, sq2 = fusion.fuse(timestamp=110.0)
    assert sq2.physio < sq.physio
    assert sq2.kinematics < sq.kinematics


def test_stale_penalty_floors_at_zero() -> None:
    # > 13s stale (threshold 3 + 10s decay window) drives quality to 0.
    fusion = FeatureFusion()
    fusion.update_physio(_physio(), timestamp=100.0)
    fusion.update_kinematics(_kin(), timestamp=100.0)
    _, sq = fusion.fuse(timestamp=100.0 + 3.0 + 10.0 + 1.0)
    assert sq.physio == 0.0
    assert sq.kinematics == 0.0
