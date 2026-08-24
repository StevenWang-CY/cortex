"""Dual-clock behavior and external timestamp semantics."""

from __future__ import annotations

from datetime import date

import pytest

from cortex.application.clock import Clock, FakeClock, SystemClock
from cortex.libs.schemas.temporal import EventTime


def test_system_clock_satisfies_port_and_shares_boot_domain() -> None:
    first = SystemClock()
    second = SystemClock()
    assert isinstance(first, Clock)
    assert first.boot_id == second.boot_id
    assert first.unix_ms() > 0
    assert first.monotonic_ns() > 0


def test_fake_clock_separates_wall_jumps_from_elapsed_time() -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=5_000)
    clock.advance(wall_ms=1_000, monotonic_ns=2_000)
    clock.jump_wall(-3_600_000)
    assert clock.monotonic_ns() == 7_000
    assert clock.unix_ms() == 1_699_996_401_000


def test_fake_clock_accepts_time_zero_and_utc_rollover() -> None:
    clock = FakeClock()
    assert clock.unix_ms() == 0
    assert clock.monotonic_ns() == 0
    assert clock.today_utc() == date(1970, 1, 1)
    clock.advance(wall_ms=86_400_000)
    assert clock.today_utc() == date(1970, 1, 2)


def test_reboot_changes_domain_and_resets_only_monotonic_clock() -> None:
    clock = FakeClock(wall_unix_ms=123_000, mono_ns=99)
    old_boot = clock.boot_id
    clock.reboot()
    assert clock.boot_id != old_boot
    assert clock.unix_ms() == 123_000
    assert clock.monotonic_ns() == 0


def test_invalid_fake_clock_movement_fails() -> None:
    with pytest.raises(ValueError):
        FakeClock(wall_unix_ms=-1)
    with pytest.raises(ValueError):
        FakeClock().advance(monotonic_ns=-1)
    with pytest.raises(ValueError):
        FakeClock().jump_wall(-1)


def test_event_time_rejects_ambiguous_or_negative_values() -> None:
    clock = FakeClock(wall_unix_ms=1000, mono_ns=2000)
    event = EventTime(
        observed_at_unix_ms=clock.unix_ms(),
        observed_at_mono_ns=clock.monotonic_ns(),
        boot_id=clock.boot_id,
    )
    assert event.schema_version == "2.0"
    with pytest.raises(ValueError):
        EventTime(
            observed_at_unix_ms=-1,
            observed_at_mono_ns=0,
            boot_id=clock.boot_id,
        )
