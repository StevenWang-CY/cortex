"""F26: quiet-mode escalation counter persists across restarts.

Pre-fix behaviour (cite trigger_policy.py:357-376 on 36cc15f): the
escalation counter reset back to 0 whenever
``now > self._quiet_mode_count_reset_at``, which was set to ``now + 2h``
on every entry into quiet mode. A user dismissing every >2h forever got
parked at level-1 (15 min) instead of escalating to 30 / 60.

After F26 lands:
- counter only resets on an explicit ``reset_quiet_mode()`` call;
- counter + last-escalation timestamp persist atomically to
  ``<config_dir>/quiet_mode_history.json`` and survive process restart.

Each case fails on ``main`` (``36cc15f``): either
``reset_quiet_mode`` does not exist, or
``quiet_mode_history_path`` is not a valid kwarg, or the 2-hour
auto-reset still fires.
"""

from __future__ import annotations

import json
from pathlib import Path

from cortex.libs.config.settings import InterventionConfig
from cortex.services.state_engine.trigger_policy import (
    QUIET_MODE_HISTORY_VERSION,
    TriggerPolicy,
)


def _make_policy(
    history_path: Path,
    *,
    dismissal_path: Path | None = None,
    window_minutes: int = 5,
    max_dismissals: int = 3,
    quiet_minutes: int = 15,
) -> TriggerPolicy:
    """Construct a policy with isolated persistence + dummy thresholds."""
    config = InterventionConfig(
        dismissal_window_minutes=window_minutes,
        max_dismissals=max_dismissals,
        quiet_mode_minutes=quiet_minutes,
    )
    return TriggerPolicy(
        config=config,
        dismissal_model_path=dismissal_path,
        quiet_mode_history_path=history_path,
    )


def _trigger_escalation(policy: TriggerPolicy, base_time: float) -> None:
    """Drive 3 dismissals inside the 5-min window to trigger one level."""
    for i in range(3):
        policy.record_dismissal(timestamp=base_time + i * 10.0)


# ---------------------------------------------------------------------------
# Case 1: escalates through levels 1 -> 2 -> 3
# ---------------------------------------------------------------------------


def test_escalates_through_three_levels(tmp_path: Path) -> None:
    """Sequential dismissal bursts escalate the counter monotonically."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal_path=dismissal)

    # First burst -> level 1 (15 min).
    _trigger_escalation(policy, base_time=1000.0)
    assert policy._quiet_mode_count == 1

    # Second burst >2h later -> on main this would reset back to 1;
    # on this branch it escalates to 2 (30 min).
    _trigger_escalation(policy, base_time=1000.0 + 3 * 3600.0)
    assert policy._quiet_mode_count == 2

    # Third burst, also >2h later -> 3 (60 min).
    _trigger_escalation(policy, base_time=1000.0 + 6 * 3600.0)
    assert policy._quiet_mode_count == 3


# ---------------------------------------------------------------------------
# Case 2: persists across "restart"
# ---------------------------------------------------------------------------


def test_counter_persists_across_restart(tmp_path: Path) -> None:
    """A second TriggerPolicy at the same history path rehydrates the level."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    first = _make_policy(history, dismissal_path=dismissal)
    _trigger_escalation(first, base_time=1000.0)
    _trigger_escalation(first, base_time=1000.0 + 3 * 3600.0)
    assert first._quiet_mode_count == 2

    # File must exist and contain a structurally-valid record.
    assert history.exists(), "quiet_mode_history.json should have been written"
    data = json.loads(history.read_text())
    assert data["version"] == QUIET_MODE_HISTORY_VERSION
    assert data["quiet_mode_count"] == 2

    # "Restart" -> new policy reading the same file.
    del first
    second = _make_policy(history, dismissal_path=dismissal)
    assert second._quiet_mode_count == 2


# ---------------------------------------------------------------------------
# Case 3: NO reset after 2h idle
# ---------------------------------------------------------------------------


def test_no_reset_after_2h_idle(tmp_path: Path) -> None:
    """The 2h auto-reset is gone: a single delayed burst still escalates."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal_path=dismissal)
    _trigger_escalation(policy, base_time=1000.0)
    assert policy._quiet_mode_count == 1

    # Wait WAY longer than 2h, then dismiss again.
    # On main, record_dismissal at this point would zero the counter
    # before incrementing, so the result would be 1 again.
    _trigger_escalation(policy, base_time=1000.0 + 12 * 3600.0)
    assert policy._quiet_mode_count == 2, (
        "Quiet-mode counter must NOT reset just because >2h passed "
        "between bursts (this was the F26 bug)."
    )


# ---------------------------------------------------------------------------
# Case 4: reset_quiet_mode() clears everything
# ---------------------------------------------------------------------------


def test_explicit_reset_clears_counter_and_file(tmp_path: Path) -> None:
    """Public reset method wipes counter, active window, and the file."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal_path=dismissal)
    _trigger_escalation(policy, base_time=1000.0)
    _trigger_escalation(policy, base_time=1000.0 + 3 * 3600.0)
    assert policy._quiet_mode_count == 2
    assert history.exists()

    policy.reset_quiet_mode()
    assert policy._quiet_mode_count == 0
    assert policy._quiet_mode_until == 0.0
    assert not history.exists(), "reset_quiet_mode must remove the history file"

    # A new policy at the same path must cold-start.
    fresh = _make_policy(history, dismissal_path=dismissal)
    assert fresh._quiet_mode_count == 0


# ---------------------------------------------------------------------------
# Case 5: escalation memory survives crash via atomic write
# ---------------------------------------------------------------------------


def test_escalation_memory_survives_crash(tmp_path: Path) -> None:
    """A simulated mid-flush crash leaves a parseable history file.

    The crash is simulated by not calling any teardown after the burst:
    the persist call writes via os.replace under the hood, so the file
    is either fully written or absent — never half-written.
    """
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal_path=dismissal)
    _trigger_escalation(policy, base_time=1000.0)
    _trigger_escalation(policy, base_time=1000.0 + 3 * 3600.0)
    _trigger_escalation(policy, base_time=1000.0 + 6 * 3600.0)
    assert policy._quiet_mode_count == 3

    # Simulate crash: drop the policy without ANY graceful shutdown.
    del policy

    # File on disk must still be complete + parseable.
    assert history.exists()
    payload = history.read_text()
    data = json.loads(payload)  # must not raise
    assert data["version"] == QUIET_MODE_HISTORY_VERSION
    assert data["quiet_mode_count"] == 3

    # No leftover ``.tmp`` artefact from atomic_write_json should be
    # present — that would indicate a torn write.
    leftover = list(tmp_path.glob("quiet_mode_history.json.tmp"))
    assert leftover == [], f"unexpected tmp file leftover: {leftover}"

    # A restart must rehydrate the saved level.
    revived = _make_policy(history, dismissal_path=dismissal)
    assert revived._quiet_mode_count == 3
