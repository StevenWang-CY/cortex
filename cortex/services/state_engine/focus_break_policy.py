"""Opt-in elapsed-focus break reminders with injected monotonic time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds


@dataclass(frozen=True, slots=True)
class FocusBreakDecision:
    status: Literal["disabled", "not_due", "due", "already_recommended", "snoozed"]
    should_recommend: bool
    reason: str
    active_elapsed_seconds: float
    preferred_interval_seconds: float
    suggested_duration_seconds: int


class FocusBreakPolicy:
    """Accumulate observed active-work time without biometric inference.

    Long gaps and inactive periods do not count. The policy owns no wall-clock
    decisions and emits at most one recommendation until the user takes a
    break, snoozes, or preferences are reset.
    """

    def __init__(
        self,
        *,
        interval_minutes: float = 50.0,
        suggested_duration_seconds: int = 300,
        enabled: bool = False,
        clock: Clock | None = None,
    ) -> None:
        if interval_minutes <= 0:
            raise ValueError("focus break interval must be positive")
        if suggested_duration_seconds <= 0:
            raise ValueError("suggested break duration must be positive")
        self._clock = clock or SYSTEM_CLOCK
        self._interval_seconds = interval_minutes * 60.0
        self._duration_seconds = suggested_duration_seconds
        self._enabled = enabled
        self._active_elapsed = 0.0
        self._last_update_at: float | None = None
        self._recommended = False
        self._snoozed_until = 0.0

    @property
    def active_elapsed_seconds(self) -> float:
        return self._active_elapsed

    def update_preferences(
        self,
        *,
        enabled: bool,
        interval_minutes: float,
        suggested_duration_seconds: int,
    ) -> None:
        if interval_minutes <= 0 or suggested_duration_seconds <= 0:
            raise ValueError("focus break preferences must be positive")
        self._enabled = enabled
        self._interval_seconds = interval_minutes * 60.0
        self._duration_seconds = suggested_duration_seconds

    def evaluate(
        self,
        *,
        active: bool,
        timestamp: float | None = None,
    ) -> FocusBreakDecision:
        supplied_now = (
            monotonic_seconds(self._clock) if timestamp is None else timestamp
        )
        now = (
            max(supplied_now, self._last_update_at)
            if self._last_update_at is not None
            else supplied_now
        )
        if self._last_update_at is not None:
            delta = now - self._last_update_at
            # Gaps usually mean suspend/sleep or a stopped sensor. They are not
            # silently counted as focused work.
            if active and 0.0 < delta <= 30.0:
                self._active_elapsed += delta
        self._last_update_at = now

        if not self._enabled:
            return self._decision("disabled", False, "focus_break_reminders_disabled")
        if now < self._snoozed_until:
            return self._decision("snoozed", False, "focus_break_reminder_snoozed")
        if self._active_elapsed < self._interval_seconds:
            return self._decision("not_due", False, "preferred_interval_not_reached")
        if self._recommended:
            return self._decision(
                "already_recommended", False, "focus_break_already_recommended"
            )
        self._recommended = True
        return self._decision("due", True, "preferred_focus_interval_reached")

    def record_break_taken(self) -> None:
        self._active_elapsed = 0.0
        self._recommended = False
        self._snoozed_until = 0.0
        self._last_update_at = None

    def snooze(self, minutes: float = 10.0, *, timestamp: float | None = None) -> None:
        if minutes <= 0:
            raise ValueError("snooze duration must be positive")
        now = monotonic_seconds(self._clock) if timestamp is None else timestamp
        self._snoozed_until = now + minutes * 60.0
        self._recommended = False

    def _decision(
        self,
        status: Literal[
            "disabled", "not_due", "due", "already_recommended", "snoozed"
        ],
        should_recommend: bool,
        reason: str,
    ) -> FocusBreakDecision:
        return FocusBreakDecision(
            status=status,
            should_recommend=should_recommend,
            reason=reason,
            active_elapsed_seconds=self._active_elapsed,
            preferred_interval_seconds=self._interval_seconds,
            suggested_duration_seconds=self._duration_seconds,
        )
