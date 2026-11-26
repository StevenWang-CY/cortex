"""An indefinite pause must be persisted when it is set and when it is lifted.

``TriggerPolicy`` rehydrates ``quiet_mode_indefinite`` from
``quiet_mode_history.json`` and the daemon adopts it at startup
(``_restore_quiet_mode_from_policy``), so "Pause all sensing" is meant to
survive a restart. But ``activate_quiet_mode(indefinite=True)`` and
``clear_quiet_mode`` never write the record; only a later dismissal or
approval does, and quiet mode blocks the proposals that would produce one.
Pausing and quitting therefore restarted the camera on the next launch, and
a stale persisted pause could come back after the user had resumed.
"""

from __future__ import annotations

from pathlib import Path

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import InterventionConfig
from cortex.services.state_engine.trigger_policy import TriggerPolicy


def _policy(tmp_path: Path) -> TriggerPolicy:
    return TriggerPolicy(
        config=InterventionConfig(),
        dismissal_model_path=tmp_path / "dismissal_model.json",
        quiet_mode_history_path=tmp_path / "quiet_mode_history.json",
        clock=FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=1_000_000_000),
    )


def test_pausing_then_restarting_keeps_the_pause(tmp_path: Path) -> None:
    _policy(tmp_path).activate_quiet_mode(indefinite=True)

    restarted = _policy(tmp_path)

    assert restarted.quiet_mode_indefinite is True


def test_resuming_then_restarting_does_not_resurrect_the_pause(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.activate_quiet_mode(indefinite=True)
    policy._persist_quiet_mode_history()  # a record with the pause exists
    policy.clear_quiet_mode()

    restarted = _policy(tmp_path)

    assert restarted.quiet_mode_indefinite is False


def test_snoozing_after_a_pause_does_not_restore_the_pause(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.activate_quiet_mode(indefinite=True)
    policy._persist_quiet_mode_history()
    policy.activate_quiet_mode(duration_minutes=15)

    restarted = _policy(tmp_path)

    assert restarted.quiet_mode_indefinite is False
