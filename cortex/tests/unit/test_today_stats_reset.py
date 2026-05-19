"""Audit-prod G3 — Today stats reset on session boundary.

The dashboard's ``_ConsumerTab._accumulate_today_stats`` was lazy-init
on first frame and never reset, so yesterday's tail leaked into
today's numerics across a daemon-restart / reconnect cycle. The fix
resets on:

1. A > 30-minute gap between STATE_UPDATEs (daemon stopped or sleep).
2. A local-date rollover (yday change).
3. Any explicit ``set_connected(False)`` (defensive — covered separately).

These tests poke the accumulator state directly because the consumer
tab requires PySide6 + offscreen Qt; we exercise the algorithm without
constructing the widget.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_tab() -> SimpleNamespace:
    """A bare object that exposes the accumulator methods + label stubs."""
    tab = SimpleNamespace()
    # The widget calls _set_text_if_changed which we stub to a no-op.
    tab._set_text_if_changed = lambda *_a, **_k: True
    # Label stubs (the algorithm doesn't read from them).
    tab._today_focus = object()
    tab._today_sessions = object()
    tab._today_best = object()
    tab._today_blocked = object()

    # Bind the methods from _ConsumerTab to this fake.
    from cortex.apps.desktop_shell.dashboard import _ConsumerTab

    tab._reset_today_stats = _ConsumerTab._reset_today_stats.__get__(tab)
    tab._accumulate_today_stats = _ConsumerTab._accumulate_today_stats.__get__(tab)
    tab._TODAY_SESSION_GAP_SECONDS = _ConsumerTab._TODAY_SESSION_GAP_SECONDS
    return tab


def test_first_call_lazy_inits(fake_tab: SimpleNamespace) -> None:
    fake_tab._accumulate_today_stats("FLOW")
    assert hasattr(fake_tab, "_today_flow_seconds")
    assert fake_tab._today_intervention_count == 0


def test_long_gap_resets_accumulators(fake_tab: SimpleNamespace) -> None:
    fake_tab._accumulate_today_stats("FLOW")
    # Manually move last_tick back by > GAP threshold.
    fake_tab._today_last_tick = (
        time.monotonic() - fake_tab._TODAY_SESSION_GAP_SECONDS - 60
    )
    fake_tab._today_flow_seconds = 1234.0
    fake_tab._today_intervention_count = 5
    fake_tab._accumulate_today_stats("FLOW")
    # After a gap, accumulator resets to a fresh session: small positive
    # FLOW seconds, intervention counter back to zero.
    assert fake_tab._today_flow_seconds < 10.0
    assert fake_tab._today_intervention_count == 0


def test_date_rollover_resets(fake_tab: SimpleNamespace) -> None:
    fake_tab._accumulate_today_stats("FLOW")
    fake_tab._today_flow_seconds = 7200.0
    fake_tab._today_intervention_count = 8
    # Fake yesterday as a different yday.
    fake_tab._today_session_yday = (
        time.localtime().tm_yday - 1
    ) or 365
    fake_tab._accumulate_today_stats("FLOW")
    assert fake_tab._today_flow_seconds < 10.0
    assert fake_tab._today_intervention_count == 0


def test_short_gap_does_not_reset(fake_tab: SimpleNamespace) -> None:
    fake_tab._accumulate_today_stats("FLOW")
    # Mimic a 5-second pause (well under threshold).
    fake_tab._today_last_tick = time.monotonic() - 5.0
    fake_tab._today_flow_seconds = 60.0
    fake_tab._today_accumulator_before = 60.0
    fake_tab._accumulate_today_stats("FLOW")
    # Accumulator should INCREASE, not reset.
    assert fake_tab._today_flow_seconds > 60.0


def test_explicit_reset_zeros_counters(fake_tab: SimpleNamespace) -> None:
    fake_tab._accumulate_today_stats("FLOW")
    fake_tab._today_flow_seconds = 999.0
    fake_tab._today_intervention_count = 7
    fake_tab._reset_today_stats()
    assert fake_tab._today_flow_seconds == 0.0
    assert fake_tab._today_intervention_count == 0
    assert fake_tab._today_best_streak == 0.0
