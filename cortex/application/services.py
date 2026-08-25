"""Instance-scoped application service composition.

The HTTP and WebSocket transports need access to a small set of runtime
services, but neither transport should own those services or discover them
through process-global state.  ``ServiceRegistry`` is therefore a composition
root primitive: one instance belongs to one running Cortex application.

String keys remain at this compatibility boundary because the existing HTTP
route catalog uses them.  New application code should depend on a narrow
``ServiceProvider`` protocol (or a concrete typed port) rather than importing a
registry singleton.
"""

from __future__ import annotations

from collections.abc import Iterator
from threading import RLock
from typing import Any, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class ServiceProvider(Protocol):
    """Read-only service lookup used by transport adapters."""

    def get(self, name: str) -> Any | None:
        """Return a service or ``None`` when it is not composed."""

    def has(self, name: str) -> bool:
        """Return whether a service is composed."""

    @property
    def registered_services(self) -> list[str]:
        """Return a stable snapshot of registered names."""

    @property
    def healthy(self) -> bool:
        """Return transport-visible application readiness."""


class ServiceRegistry:
    """Thread-safe, instance-scoped service container.

    Registration is intentionally confined to the composition root.  The
    lock matters in in-process desktop mode, where Qt and the daemon can read
    diagnostic state from different threads.
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._healthy = False
        self._lock = RLock()

    def register(self, name: str, service: Any) -> None:
        if not name or not name.strip():
            raise ValueError("service name must be non-empty")
        with self._lock:
            self._services[name] = service

    def get(self, name: str) -> Any | None:
        with self._lock:
            return self._services.get(name)

    def require(self, name: str, expected_type: type[T]) -> T:
        """Resolve one service and enforce its runtime boundary type."""

        value = self.get(name)
        if value is None:
            raise LookupError(f"required service is not registered: {name}")
        if not isinstance(value, expected_type):
            raise TypeError(
                f"service {name!r} is {type(value).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return value

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._services

    @property
    def registered_services(self) -> list[str]:
        with self._lock:
            return list(self._services)

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @healthy.setter
    def healthy(self, value: bool) -> None:
        with self._lock:
            self._healthy = bool(value)

    def reset(self) -> None:
        with self._lock:
            self._services.clear()
            self._healthy = False

    def items(self) -> Iterator[tuple[str, Any]]:
        """Iterate over a point-in-time snapshot for diagnostics."""

        with self._lock:
            snapshot = tuple(self._services.items())
        return iter(snapshot)
