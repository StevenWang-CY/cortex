"""An indefinite "Pause all sensing" must survive a daemon restart.

``TriggerPolicy`` persists ``quiet_mode_indefinite`` and rehydrates it in
``__init__`` precisely because an indefinite pause is a standing user decision
rather than a timed window. The daemon's own mirror of that state
(``_quiet_mode_kind``) was rebuilt as ``"off"`` on every construction, so after
a restart the UI, the WS envelope and the tray all reported "off" while the
policy still suppressed triggers — and, worse, the camera was started again.
The user saw the camera light come back on by itself after asking for sensing
to stop.

Timed windows are deliberately NOT restored: they are measured against a
monotonic clock, which does not survive the process.
"""

from __future__ import annotations

import json

import pytest

from cortex.services.state_engine.trigger_policy import (
    QUIET_MODE_HISTORY_VERSION,
    TriggerPolicy,
)


def _policy_with_history(tmp_path, payload: dict) -> TriggerPolicy:
    history = tmp_path / "quiet_mode_history.json"
    record = {"version": QUIET_MODE_HISTORY_VERSION, "quiet_mode_count": 0}
    record.update(payload)
    history.write_text(json.dumps(record), encoding="utf-8")
    return TriggerPolicy(quiet_mode_history_path=history)


def test_policy_rehydrates_an_indefinite_pause(tmp_path) -> None:
    policy = _policy_with_history(tmp_path, {"quiet_mode_indefinite": True})
    assert policy.quiet_mode_indefinite is True
    assert policy.is_quiet_mode is True


def test_policy_does_not_invent_a_pause(tmp_path) -> None:
    policy = _policy_with_history(tmp_path, {"quiet_mode_indefinite": False})
    assert policy.quiet_mode_indefinite is False


@pytest.mark.parametrize("indefinite", [True, False])
def test_daemon_adopts_the_policys_restored_pause(
    tmp_path, monkeypatch: pytest.MonkeyPatch, indefinite: bool,
) -> None:
    """The daemon's quiet-mode mirror must agree with the policy at startup."""
    from cortex.libs.config.settings import CortexConfig
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=CortexConfig())
    # Stand in for the policy the daemon built during __init__, exactly as a
    # rehydrated one would look.
    daemon._trigger_policy = _policy_with_history(
        tmp_path, {"quiet_mode_indefinite": indefinite},
    )
    assert daemon._quiet_mode_kind == "off", "precondition: mirror starts off"

    daemon._restore_quiet_mode_from_policy()

    if indefinite:
        assert daemon._quiet_mode_kind == "pause"
        assert daemon._quiet_mode_ends_at is None
        assert daemon._quiet_mode_source == "restored"
        # So that resuming actually turns the camera back on.
        assert daemon._pause_was_capturing is True
    else:
        assert daemon._quiet_mode_kind == "off"
        assert daemon._quiet_mode_source == "daemon"
