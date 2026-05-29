"""
Consent Engine — Policy Definitions

Maps action types to their minimum required consent levels.
This is the configuration layer that defines how conservative
Cortex should be for each type of workspace mutation.
"""

from __future__ import annotations

from typing import Any

# Consent levels (mirrors ConsentLevel IntEnum)
OBSERVE = 0
SUGGEST = 1
PREVIEW = 2
REVERSIBLE_ACT = 3
AUTONOMOUS_ACT = 4

# Default minimum consent levels for each action type
DEFAULT_ACTION_LEVELS: dict[str, int] = {
    # Low-risk: suggestion is sufficient
    "show_overlay": SUGGEST,
    "highlight_tab": SUGGEST,
    "start_timer": SUGGEST,
    "copy_to_clipboard": SUGGEST,
    "show_breathing": SUGGEST,
    "show_active_recall": SUGGEST,

    # Medium-risk: preview required
    "close_tab": PREVIEW,
    "group_tabs": PREVIEW,
    "bookmark_and_close": PREVIEW,
    "save_session": PREVIEW,
    "fold_code": PREVIEW,
    "search_error": PREVIEW,

    # Higher-risk: reversible act (user must have approved this type before)
    "open_url": REVERSIBLE_ACT,
    "disable_copilot": REVERSIBLE_ACT,
    "enable_copilot": REVERSIBLE_ACT,
    "bring_file_forward": REVERSIBLE_ACT,
    "minimize_window": REVERSIBLE_ACT,
    "collapse_code": REVERSIBLE_ACT,

    # Highest-risk: autonomous act (requires extensive trust)
    "shutdown_workspace": AUTONOMOUS_ACT,
    "launch_project": AUTONOMOUS_ACT,
    "hide_distraction_apps": AUTONOMOUS_ACT,

    # P0 §3.5/§3.6: re-engage + micro-step actions
    "resume_last_active_file": REVERSIBLE_ACT,
    "prompt_micro_commit": SUGGEST,
    "suggest_movement_break": SUGGEST,

    # P0 §3.7: biology-driven break action. SUGGEST is sufficient — the
    # action only shows a full-screen breathing overlay on the user's
    # own display (no workspace mutation; no destructive side effect).
    # The user can end the session at any time, so even REVERSIBLE_ACT
    # would be over-cautious here.
    "take_biology_break": SUGGEST,

    # P0 §3.10: distraction blocking as an action class.
    # Default REVERSIBLE_ACT — the user can disarm in one click any
    # time. The user-visible "Auto-arm in HYPER" toggle in Settings
    # → Focus protection sets the level to AUTONOMOUS_ACT, so the
    # daemon's HYPER → START_FOCUS_AUTO path only fires for users
    # who explicitly opted in. The minimum bar is REVERSIBLE_ACT
    # rather than PREVIEW because no destructive mutation happens —
    # the interstitial is non-destructive and one click bypasses it.
    "distraction_block": REVERSIBLE_ACT,

    # P1: planner/executor ADAPTER-level action names. The executor's
    # per-action consent gate (``InterventionExecutor.apply``) checks
    # the consent ladder using ``cmd.action`` — the adapter-level verb
    # the planner emits — not the canonical policy verbs above. Those
    # adapter names were previously absent from the vocabulary, so
    # ``get_minimum_level`` fell through to the PREVIEW default and the
    # escalation ladder (which tracks per-action-type) could never lift
    # them above PREVIEW. Registering them here gives them a real
    # minimum level so approvals recorded against the same canonical key
    # escalate the gate the executor actually queries. See
    # ``canonical_action_type`` for the executor/daemon mapping.
    "hide_tabs_except_active": PREVIEW,
    "collapse_before_error": PREVIEW,
    "fold_except_current": PREVIEW,
    "dim_background": SUGGEST,
}


# ---------------------------------------------------------------------------
# Adapter-action → canonical policy action-type mapping (P1)
# ---------------------------------------------------------------------------
#
# The intervention planner emits ADAPTER-level command verbs
# (``cmd.action``: e.g. ``hide_tabs_except_active``) while the consent
# ladder's escalation history is keyed by action-type. Before the fix the
# executor gate called ``check(cmd.action, ...)`` while the daemon
# recorded approvals/rejections under the literal string ``"intervention"``
# — two different keys, so a user approving N interventions never lifted
# the gate on the adapter actions the executor actually queries.
#
# ``canonical_action_type`` collapses the (small, closed) set of adapter
# verbs onto the stable policy key used for BOTH the gate check and the
# approval/rejection recording. Verbs with a 1:1 policy entry map to
# themselves; the legacy adapter aliases map onto their canonical policy
# verb. Anything unknown passes through unchanged (so a future verb still
# gates at its own key rather than silently collapsing into another).
_ADAPTER_ACTION_ALIASES: dict[str, str] = {
    # Tab simplification adapter verb → canonical tab-grouping policy verb.
    "hide_tabs_except_active": "group_tabs",
    # Terminal collapse adapter verb → canonical code-fold policy verb.
    "collapse_before_error": "fold_code",
    # Editor fold adapter verb → canonical code-fold policy verb.
    "fold_except_current": "fold_code",
}


def canonical_action_type(action: str) -> str:
    """Map an adapter-level action verb to its canonical policy action-type.

    Used by the executor's per-action consent gate so the check and the
    daemon-side approval/rejection recording share one stable key, letting
    the escalation ladder actually lift the gate on a mapped action after
    enough approvals. Unknown verbs pass through unchanged.
    """
    return _ADAPTER_ACTION_ALIASES.get(action, action)


class ConsentPolicy:
    """
    Policy defining minimum consent levels for action types.

    Can be customized by the user to be more or less conservative.
    """

    def __init__(
        self,
        overrides: dict[str, int] | None = None,
        global_max_level: int = REVERSIBLE_ACT,
    ) -> None:
        self._levels = dict(DEFAULT_ACTION_LEVELS)
        if overrides:
            self._levels.update(overrides)
        self._global_max = global_max_level

    def get_minimum_level(self, action_type: str) -> int:
        """Get the minimum consent level required for an action type."""
        return self._levels.get(action_type, PREVIEW)

    def set_level(self, action_type: str, level: int) -> None:
        """Override the consent level for an action type."""
        self._levels[action_type] = max(0, min(4, level))

    @property
    def global_max_level(self) -> int:
        """Global maximum consent level (user-configurable cap)."""
        return self._global_max

    @global_max_level.setter
    def global_max_level(self, value: int) -> None:
        self._global_max = max(0, min(4, value))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "levels": dict(self._levels),
            "global_max": self._global_max,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsentPolicy:
        """Restore from serialized state."""
        return cls(
            overrides=data.get("levels"),
            global_max_level=data.get("global_max", REVERSIBLE_ACT),
        )
