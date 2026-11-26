"""A machine sleep between two state ticks must not be credited as flow.

The daemon feeds ``SessionReportGenerator.record_state`` on every 2 Hz state
tick with wall-clock seconds and the generator credits the whole interval
since the previous tick to the previous label. When a laptop sleeps with
Cortex running (lid closed overnight) the first tick after wake therefore
credits the entire sleep to whatever label was current when the lid closed:
History and the trends chart report hours of FLOW and an hours-long
"longest flow streak" the user never had. AGENTS rule 39: missing time
cannot earn dwell.

The existing contracts in ``test_session_report.py`` credit sparse
transition-style calls up to 160 s apart, so the bound here is generous:
any bound between a few minutes and an hour satisfies both.
"""

from __future__ import annotations

from cortex.services.session_report.generator import SessionReportGenerator

_TICK = 0.5
_SLEEP = 8 * 3600.0
_GENEROUS_BOUND = 3600.0


def _ticks(gen: SessionReportGenerator, state: str, start: float, count: int) -> float:
    t = start
    for _ in range(count):
        gen.record_state(state, t)
        t += _TICK
    return t - _TICK


def test_overnight_sleep_between_ticks_is_not_flow() -> None:
    gen = SessionReportGenerator()
    gen.start()
    t0 = 1_800_000_000.0
    last_flow_tick = _ticks(gen, "FLOW", t0, 120)  # one minute of real flow
    wake = last_flow_tick + _SLEEP
    _ticks(gen, "UNKNOWN", wake, 10)

    report = gen.finish(end_timestamp=wake + 5.0)

    assert report.time_in_flow_seconds < _GENEROUS_BOUND, report.time_in_flow_seconds
    assert report.longest_flow_streak_seconds < _GENEROUS_BOUND, (
        report.longest_flow_streak_seconds
    )


def test_flow_on_both_sides_of_a_sleep_is_two_streaks_of_observed_flow() -> None:
    gen = SessionReportGenerator()
    gen.start()
    t0 = 1_800_000_000.0
    before = _ticks(gen, "FLOW", t0, 121)  # 60 s of flow
    after = _ticks(gen, "FLOW", before + _SLEEP, 61)  # 30 s of flow after waking

    report = gen.finish(end_timestamp=after)

    assert report.time_in_flow_seconds == 90.0
    assert report.longest_flow_streak_seconds == 60.0
    assert [a["kind"] for a in gen.clock_anomalies] == ["unobserved_gap"]


def test_sleep_while_flow_is_the_last_label_before_stop_is_not_flow() -> None:
    gen = SessionReportGenerator()
    gen.start()
    t0 = 1_800_000_000.0
    last_flow_tick = _ticks(gen, "FLOW", t0, 120)

    # The user wakes the Mac and stops the session at once.
    report = gen.finish(end_timestamp=last_flow_tick + _SLEEP)

    assert report.time_in_flow_seconds < _GENEROUS_BOUND, report.time_in_flow_seconds
