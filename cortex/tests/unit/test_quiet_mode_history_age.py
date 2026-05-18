"""audit-w2 — F26's persisted last-escalation age must be a real positive
delta.

The original F26 persistence code computed
``self._quiet_mode_count_reset_at - time.monotonic()`` for the
``last_escalation_at_monotonic_delta`` field. Because
``_quiet_mode_count_reset_at`` is the monotonic timestamp of the LAST
escalation (always in the past once one has happened), the expression
was always negative, and the surrounding ``max(0.0, ...)`` clamped it
to 0. The persisted field was therefore always 0, and on rehydrate the
load path stamped ``_quiet_mode_count_reset_at = time.monotonic()`` —
i.e. "the last escalation was just now", regardless of when it actually
fired.

The field is currently only used for diagnostics, so the consumer-
visible behaviour was unchanged — but the next reader to trust the
value (planned dashboard "last escalation N hours ago" affordance)
would have inherited the bug. audit-w2 inverts the sign and renames
the field to ``last_escalation_age_seconds`` to make the intent
obvious; the loader accepts both the new name and the legacy name for
forward-compat with already-on-disk records.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cortex.libs.config.settings import InterventionConfig
from cortex.services.state_engine.trigger_policy import (
    QUIET_MODE_HISTORY_VERSION,
    TriggerPolicy,
)


def _make_policy(history: Path, dismissal: Path) -> TriggerPolicy:
    return TriggerPolicy(
        config=InterventionConfig(
            dismissal_window_minutes=5,
            max_dismissals=3,
            quiet_mode_minutes=15,
        ),
        dismissal_model_path=dismissal,
        quiet_mode_history_path=history,
    )


def _escalate(policy: TriggerPolicy, t: float) -> None:
    for i in range(3):
        policy.record_dismissal(timestamp=t + i * 10.0)


def test_persisted_age_is_positive_after_real_delay(tmp_path: Path) -> None:
    """Drive one escalation with real monotonic timestamps, wait a tick,
    persist again, and assert the age field is non-zero (the legacy
    0-clamp bug is gone)."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal)

    # First escalation stamps reset_at = current monotonic. Three
    # close-together dismissals fit inside the 5-min dismissal_window
    # without offsetting reset_at into the future.
    t0 = time.monotonic()
    for i in range(3):
        policy.record_dismissal(timestamp=t0 + i * 0.001)

    # Sleep a measurable interval so reset_at is now in the past.
    time.sleep(0.05)
    policy._persist_quiet_mode_history()  # noqa: SLF001

    data = json.loads(history.read_text())
    assert data["version"] == QUIET_MODE_HISTORY_VERSION
    # Field renamed in audit-w2; the legacy field is no longer written.
    assert "last_escalation_age_seconds" in data
    assert data["last_escalation_age_seconds"] >= 0.04, (
        "Expected a positive age (~0.05s); bugged code persisted 0.0"
    )
    assert "last_escalation_at_monotonic_delta" not in data, (
        "Legacy negative-delta field should not be written by the new code"
    )


def test_zero_when_no_escalation_yet(tmp_path: Path) -> None:
    """If no escalation has fired, the age is 0 (sentinel), not a
    spurious huge number from ``monotonic() - 0``."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal)
    policy._persist_quiet_mode_history()  # noqa: SLF001
    data = json.loads(history.read_text())
    assert data["last_escalation_age_seconds"] == 0.0


def test_loader_accepts_legacy_field_name(tmp_path: Path) -> None:
    """An on-disk file written by the pre-audit-w2 code path uses the
    legacy ``last_escalation_at_monotonic_delta`` key with the bugged
    0.0 value. The loader must still rehydrate the counter."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    legacy = {
        "version": QUIET_MODE_HISTORY_VERSION,
        "quiet_mode_count": 2,
        "quiet_mode_until_monotonic_delta": 0.0,
        "last_escalation_at_monotonic_delta": 0.0,
        "saved_at": time.time(),
    }
    history.write_text(json.dumps(legacy))

    policy = _make_policy(history, dismissal)
    assert policy._quiet_mode_count == 2, (
        "Rehydration must work against legacy field name"
    )


def test_age_round_trips_through_save_and_load(tmp_path: Path) -> None:
    """Save, then load, and assert the rehydrated ``_quiet_mode_count_reset_at``
    is in the past — i.e. the sign survived the round-trip."""
    history = tmp_path / "quiet_mode_history.json"
    dismissal = tmp_path / "dismissal_model.json"
    policy = _make_policy(history, dismissal)
    t0 = time.monotonic()
    for i in range(3):
        policy.record_dismissal(timestamp=t0 + i * 0.001)
    time.sleep(0.05)
    policy._persist_quiet_mode_history()  # noqa: SLF001

    # Restart.
    del policy
    revived = _make_policy(history, dismissal)
    # ``_quiet_mode_count_reset_at`` must be strictly less than
    # ``time.monotonic()`` (i.e. in the past). The legacy bug rehydrated
    # it to ``time.monotonic()`` (now).
    now = time.monotonic()
    assert revived._quiet_mode_count_reset_at < now, (
        f"reset_at ({revived._quiet_mode_count_reset_at}) must be in "
        f"the past relative to now ({now})"
    )
