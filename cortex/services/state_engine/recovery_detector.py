"""
State Engine — RECOVERY Reinforcement Window (P0 §3.5).

After the user exits HYPER (overwhelm), the next ~5 minutes are
**protective**, not generative. Shoving a new "and now do X" plan at
someone who just clawed out of overwhelm is exactly the wrong move; the
design says reinforce ("nice — keep going") instead. This module gates
that reinforcement so:

1. We only reinforce while the user is recently-emerged from HYPER
   (within ``recovery_window_seconds``).
2. We reinforce *at most once* per recovery window, even if the trigger
   policy is invoked at 2 Hz the whole time. The cooldown is internal to
   the detector — independent of the policy-level cooldown which gates
   *all* states uniformly.

The reinforcement carries ``intervention_type="overlay_only"`` and
``tone="minimal"`` via the prompt template — see
:mod:`cortex.services.llm_engine.prompts`. The overlay must NOT block
input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Module-level defaults.
# ---------------------------------------------------------------------------

# The window during which a recently-emerged-from-HYPER user is eligible
# for a reinforcement message. 5 minutes balances "long enough that the
# user notices the all-clear" against "short enough that we don't haunt
# the session with stale congratulations".
DEFAULT_RECOVERY_WINDOW_SECONDS: float = 300.0

# Minimum seconds between successive reinforcements *within the same
# window*. Set equal to the window so by default we fire exactly once.
DEFAULT_REINFORCE_COOLDOWN_SECONDS: float = 300.0


@dataclass(frozen=True)
class RecoveryGateConfig:
    """Tunables for the RECOVERY reinforcement gate."""

    window_seconds: float = DEFAULT_RECOVERY_WINDOW_SECONDS
    reinforce_cooldown_seconds: float = DEFAULT_REINFORCE_COOLDOWN_SECONDS


def in_recovery_window(
    features: Any,
    *,
    just_exited_hyper: bool,
    seconds_since_exit: float,
    config: RecoveryGateConfig | None = None,
) -> tuple[bool, str]:
    """Return whether the user is inside the protective recovery window.

    Args:
        features: Live feature vector (unused today; reserved for future
            "did the user actually settle back into FLOW" gating).
        just_exited_hyper: True iff the user transitioned out of HYPER
            (into FLOW or RECOVERY) recently. Sourced from the state
            engine's transition tracker.
        seconds_since_exit: Wall-time seconds since that exit.
        config: Optional override for the module-level defaults.

    Returns:
        ``(in_window, reason)``. ``reason`` is a short human-readable
        label suitable for :class:`TriggerDecision.reason`.
    """
    cfg = config or RecoveryGateConfig()

    if not just_exited_hyper:
        return (False, "Not recently emerged from HYPER")

    if seconds_since_exit < 0:
        # Defensive — a negative delta means the caller passed garbage.
        # Treat as out-of-window so we never spuriously reinforce.
        return (False, "Recovery delta is negative")

    if seconds_since_exit > cfg.window_seconds:
        return (
            False,
            f"Recovery window expired ({seconds_since_exit:.0f}s > "
            f"{cfg.window_seconds:.0f}s)",
        )

    return (True, "RECOVERY reinforcement window active")


class RecoveryReinforcer:
    """Stateful "fire at most once per window" gate.

    The trigger policy holds one of these per session. ``should_reinforce``
    is called whenever a RECOVERY trigger evaluation gets past the
    behavioural gate; the helper returns True on the first call inside a
    window and False on every subsequent call until the window resets
    (via :meth:`reset_window`).
    """

    def __init__(self, config: RecoveryGateConfig | None = None) -> None:
        self._config = config or RecoveryGateConfig()
        self._last_reinforced_dwell: float | None = None

    def reset_window(self) -> None:
        """Forget the previous reinforcement so the next call may fire."""
        self._last_reinforced_dwell = None

    def should_reinforce(self, *, dwell_seconds: float) -> bool:
        """Return True if a reinforcement is allowed at this dwell.

        Args:
            dwell_seconds: Seconds the user has been in RECOVERY. Used
                as the cooldown clock — once we reinforce, subsequent
                calls within ``reinforce_cooldown_seconds`` of the same
                dwell line are suppressed.

        Returns:
            True the first time the gate opens inside a window; False
            for every subsequent call until :meth:`reset_window` is
            invoked (which the trigger policy does when the user
            transitions back into HYPER).
        """
        last = self._last_reinforced_dwell
        if last is None:
            self._last_reinforced_dwell = dwell_seconds
            return True
        if (dwell_seconds - last) >= self._config.reinforce_cooldown_seconds:
            self._last_reinforced_dwell = dwell_seconds
            return True
        return False


# Module-level singleton used by the bare-function call form in
# ``trigger_policy``. Tests that need isolation should construct their
# own :class:`RecoveryReinforcer`.
_DEFAULT_REINFORCER = RecoveryReinforcer()


def should_reinforce(*, dwell_seconds: float) -> bool:
    """Functional shim that delegates to the module singleton.

    Mirrors the design-doc signature. Stateful tests should prefer
    instantiating :class:`RecoveryReinforcer` directly so they don't
    bleed state across runs.
    """
    return _DEFAULT_REINFORCER.should_reinforce(dwell_seconds=dwell_seconds)


def _reset_default_reinforcer_for_tests() -> None:
    """Test-only: clear the module singleton between cases."""
    _DEFAULT_REINFORCER.reset_window()
