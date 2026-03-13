"""
Consent Engine — Policy Definitions

Maps action types to their minimum required consent levels.
This is the configuration layer that defines how conservative
Cortex should be for each type of workspace mutation.
"""

from __future__ import annotations

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
}


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

    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "levels": dict(self._levels),
            "global_max": self._global_max,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConsentPolicy:
        """Restore from serialized state."""
        return cls(
            overrides=data.get("levels"),
            global_max_level=data.get("global_max", REVERSIBLE_ACT),
        )
