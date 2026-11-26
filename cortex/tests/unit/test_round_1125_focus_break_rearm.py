"""An opt-in focus-break reminder must re-arm after the user takes a break.

``FocusBreakPolicy`` emits "at most one recommendation until the user takes
a break, snoozes, or preferences are reset". In the product only a
*completed guided break* calls ``record_break_taken``: nothing calls
``snooze`` and ``update_preferences`` (run every tick) never clears the
latch, while the reminder itself is a browser toast with no reply path. A
user who gets the reminder and simply steps away for longer than the
suggested break therefore never receives another reminder for the rest of
the daemon's life.
"""

from __future__ import annotations

from cortex.application.clock import FakeClock
from cortex.services.state_engine.focus_break_policy import FocusBreakPolicy

_INTERVAL_MIN = 15.0
_BREAK_S = 300


def _run(
    policy: FocusBreakPolicy, *, start: float, seconds: int, active: bool | None
) -> tuple[float, bool]:
    due = False
    t = start
    for _ in range(seconds):
        t += 1.0
        due = policy.evaluate(active=active, timestamp=t).should_recommend or due
    return t, due


def test_a_real_break_longer_than_the_suggested_one_rearms_the_reminder() -> None:
    policy = FocusBreakPolicy(
        interval_minutes=_INTERVAL_MIN,
        suggested_duration_seconds=_BREAK_S,
        enabled=True,
        clock=FakeClock(),
    )
    t, due = _run(policy, start=1_000.0, seconds=int(_INTERVAL_MIN * 60) + 5, active=True)
    assert due, "precondition: the first reminder becomes due"
    policy.record_recommended()  # the daemon presented it

    # The user steps away (observed inactivity) for twice the suggested break.
    t, _ = _run(policy, start=t, seconds=2 * _BREAK_S, active=False)
    # ... then works another full interval.
    _t, due_again = _run(policy, start=t, seconds=int(_INTERVAL_MIN * 60) + 5, active=True)

    assert due_again, "the reminder never re-armed after a real break"


def _due_after_first_reminder(policy: FocusBreakPolicy) -> float:
    t, due = _run(policy, start=1_000.0, seconds=int(_INTERVAL_MIN * 60) + 5, active=True)
    assert due
    policy.record_recommended()
    return t


def _policy() -> FocusBreakPolicy:
    return FocusBreakPolicy(
        interval_minutes=_INTERVAL_MIN,
        suggested_duration_seconds=_BREAK_S,
        enabled=True,
        clock=FakeClock(),
    )


def test_unavailable_input_is_not_a_break() -> None:
    # Rule 38: a stopped input listener is not the user stepping away.
    policy = _policy()
    t = _due_after_first_reminder(policy)
    t, _ = _run(policy, start=t, seconds=2 * _BREAK_S, active=None)
    _t, due_again = _run(policy, start=t, seconds=int(_INTERVAL_MIN * 60) + 5, active=True)
    assert not due_again


def test_short_pauses_do_not_add_up_to_a_break() -> None:
    policy = _policy()
    t = _due_after_first_reminder(policy)
    for _ in range(6):
        t, _ = _run(policy, start=t, seconds=_BREAK_S // 2, active=False)
        t, _ = _run(policy, start=t, seconds=5, active=True)
    assert policy.evaluate(active=True, timestamp=t + 1.0).status == "already_recommended"
