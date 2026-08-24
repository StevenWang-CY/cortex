"""Explicit dual-clock port for deterministic domain behavior."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

_PROCESS_BOOT_ID = uuid4()


@runtime_checkable
class Clock(Protocol):
    """Clock capabilities permitted in migrated domain services."""

    def unix_ms(self) -> int:
        """Return UTC Unix epoch milliseconds for wire and persistence."""

    def monotonic_ns(self) -> int:
        """Return process-local monotonic nanoseconds for elapsed time."""

    def today_utc(self) -> date:
        """Return the UTC calendar date derived from wall time."""

    @property
    def boot_id(self) -> UUID:
        """Identify the process boot domain of monotonic timestamps."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production clock; all instances share one process boot identifier."""

    @property
    def boot_id(self) -> UUID:
        return _PROCESS_BOOT_ID

    def unix_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def today_utc(self) -> date:
        return datetime.fromtimestamp(self.unix_ms() / 1000, tz=UTC).date()


@dataclass(slots=True)
class FakeClock:
    """Independently controlled wall/monotonic clock for deterministic tests."""

    wall_unix_ms: int = 0
    mono_ns: int = 0
    _boot_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.wall_unix_ms < 0 or self.mono_ns < 0:
            raise ValueError("fake clock values must be non-negative")

    @property
    def boot_id(self) -> UUID:
        return self._boot_id

    def unix_ms(self) -> int:
        return self.wall_unix_ms

    def monotonic_ns(self) -> int:
        return self.mono_ns

    def today_utc(self) -> date:
        return datetime.fromtimestamp(self.wall_unix_ms / 1000, tz=UTC).date()

    def advance(self, *, wall_ms: int = 0, monotonic_ns: int = 0) -> None:
        """Advance either clock; negative movement is forbidden here."""

        if wall_ms < 0 or monotonic_ns < 0:
            raise ValueError("advance deltas must be non-negative")
        self.wall_unix_ms += wall_ms
        self.mono_ns += monotonic_ns

    def jump_wall(self, delta_ms: int) -> None:
        """Simulate an NTP/manual wall-clock jump in either direction."""

        target = self.wall_unix_ms + delta_ms
        if target < 0:
            raise ValueError("wall clock cannot precede Unix epoch")
        self.wall_unix_ms = target

    def reboot(self, *, monotonic_ns: int = 0) -> None:
        """Start a new monotonic domain while retaining wall time."""

        if monotonic_ns < 0:
            raise ValueError("monotonic_ns must be non-negative")
        self.mono_ns = monotonic_ns
        self._boot_id = uuid4()
