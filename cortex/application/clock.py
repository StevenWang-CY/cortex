"""Explicit dual-clock port and deadline arithmetic.

Wall time is for persistence/display. Monotonic time is for elapsed-time
decisions, and may only be compared inside one :attr:`Clock.boot_id` domain.
Keeping those rules here prevents individual services from inventing unsafe
fallbacks when the wall clock moves or the process restarts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, Protocol, runtime_checkable
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


SYSTEM_CLOCK: Final[Clock] = SystemClock()
"""Process-wide production clock used at compatibility boundaries.

Domain services should still receive a ``Clock`` in their constructor. This
singleton exists for framework/Pydantic default factories whose construction
is outside the composition root.
"""


def unix_seconds(clock: Clock) -> float:
    """Return epoch seconds only for legacy compatibility fields."""

    return clock.unix_ms() / 1000.0


def monotonic_seconds(clock: Clock) -> float:
    """Return monotonic seconds for legacy internal APIs during migration."""

    return clock.monotonic_ns() / 1_000_000_000.0


def utc_datetime(clock: Clock) -> datetime:
    """Return a timezone-aware UTC datetime derived from ``clock``."""

    return datetime.fromtimestamp(unix_seconds(clock), tz=UTC)


def clock_or_system(candidate: object) -> Clock:
    """Return a valid injected clock or the compatibility boundary clock.

    Structural mocks satisfy a runtime-checkable protocol surprisingly
    easily: ``MagicMock`` fabricates every requested method and property.
    Requiring a real UUID clock domain prevents such objects from leaking
    mock values into serialized contracts while still accepting alternate
    production/test ``Clock`` implementations.
    """

    if isinstance(candidate, Clock) and isinstance(candidate.boot_id, UUID):
        return candidate
    return SYSTEM_CLOCK


@dataclass(frozen=True, slots=True)
class BoundedDeadline:
    """Persistable deadline that cannot be extended by clock rollback.

    ``expires_at_unix_ms`` preserves restart behavior. ``duration_ms`` and
    the monotonic anchor cap the remaining lifetime during the originating
    boot. After reboot, the original duration remains the upper bound even if
    the wall clock moved backwards while Cortex was stopped.
    """

    expires_at_unix_ms: int
    duration_ms: int
    created_at_unix_ms: int
    created_at_mono_ns: int
    boot_id: UUID

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        if self.created_at_unix_ms < 0 or self.created_at_mono_ns < 0:
            raise ValueError("deadline anchors must be non-negative")
        if self.expires_at_unix_ms < self.created_at_unix_ms:
            raise ValueError("deadline expiry cannot precede creation")

    @classmethod
    def after(cls, clock: Clock, duration_ms: int) -> BoundedDeadline:
        """Create a deadline ``duration_ms`` from the supplied clock."""

        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        wall = clock.unix_ms()
        return cls(
            expires_at_unix_ms=wall + duration_ms,
            duration_ms=duration_ms,
            created_at_unix_ms=wall,
            created_at_mono_ns=clock.monotonic_ns(),
            boot_id=clock.boot_id,
        )

    def remaining_ms(self, clock: Clock) -> int:
        """Return remaining lifetime, bounded under wall jumps and reboot."""

        wall_remaining = max(0, self.expires_at_unix_ms - clock.unix_ms())
        if clock.boot_id != self.boot_id:
            return min(self.duration_ms, wall_remaining)

        elapsed_ns = max(0, clock.monotonic_ns() - self.created_at_mono_ns)
        mono_remaining = max(0, self.duration_ms - elapsed_ns // 1_000_000)
        return min(wall_remaining, mono_remaining)

    def expired(self, clock: Clock) -> bool:
        """Return whether no bounded lifetime remains."""

        return self.remaining_ms(clock) == 0

    def to_record(self) -> dict[str, int | str]:
        """Return a JSON-safe persistence record."""

        return {
            "expires_at_unix_ms": self.expires_at_unix_ms,
            "duration_ms": self.duration_ms,
            "created_at_unix_ms": self.created_at_unix_ms,
            "created_at_mono_ns": self.created_at_mono_ns,
            "boot_id": str(self.boot_id),
        }

    @classmethod
    def from_record(cls, value: dict[str, object]) -> BoundedDeadline:
        """Validate and reconstruct a persisted deadline record."""

        return cls(
            expires_at_unix_ms=int(str(value["expires_at_unix_ms"])),
            duration_ms=int(str(value["duration_ms"])),
            created_at_unix_ms=int(str(value["created_at_unix_ms"])),
            created_at_mono_ns=int(str(value["created_at_mono_ns"])),
            boot_id=UUID(str(value["boot_id"])),
        )
