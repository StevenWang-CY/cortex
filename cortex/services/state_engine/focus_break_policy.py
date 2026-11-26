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

    A break is either a completed guided break (:meth:`record_break_taken`)
    or *observed* inactivity lasting the suggested break duration. The
    reminder is a toast with no reply path, so without the second form only a
    guided break re-armed it and a user who simply stepped away never got a
    second reminder for the rest of the daemon's life. Unavailable input
    (``active=None``) and unobserved gaps are not inactivity (AGENTS rule 38)
    and interrupt a run of it.
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
        self._inactive_run = 0.0

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
        active: bool | None,
        timestamp: float | None = None,
    ) -> FocusBreakDecision:
        """Advance elapsed focus; ``active=None`` means input is unavailable."""
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
            # silently counted as focused work, nor as a break.
            observed = 0.0 < delta <= 30.0
            if active is True and observed:
                self._active_elapsed += delta
            if active is False and observed:
                self._inactive_run += delta
            elif active is not False or delta > 30.0:
                self._inactive_run = 0.0
            if self._inactive_run >= self._duration_seconds:
                # The user stepped away for the suggested break: the focus
                # interval starts over and the next reminder is earned anew.
                self._active_elapsed = 0.0
                self._recommended = False
                self._inactive_run = 0.0
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
        # Deliberately does NOT consume the one-shot budget. ``evaluate`` is
        # advisory: the caller still has to clear the shared interruption gate
        # (quiet mode, pause, snooze, receptivity, schedule, cooldown, hourly
        # cap) before anything is shown. Setting ``_recommended`` here burned
        # the reminder whenever that gate said no, and every later evaluate
        # returned "already_recommended" until a break was taken or snoozed --
        # so a user who was merely in cooldown lost the reminder for the whole
        # interval and never saw it. The surface that actually presents the
        # reminder calls ``record_recommended``, mirroring the same
        # present-then-record contract as ``TriggerPolicy.record_intervention``.
        return self._decision("due", True, "preferred_focus_interval_reached")

    def record_recommended(self) -> None:
        """Consume the one-shot budget for the current focus interval.

        Called by the surface that presented the reminder, never by
        ``evaluate``. Until it is called, a gated reminder stays due.
        """

        self._recommended = True

    def record_break_taken(self) -> None:
        self._active_elapsed = 0.0
        self._recommended = False
        self._snoozed_until = 0.0
        self._last_update_at = None
        self._inactive_run = 0.0

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
