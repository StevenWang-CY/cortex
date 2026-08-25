"""Typed in-process application events with explicit subscriptions."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Generic, TypeVar
from uuid import uuid4

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Subscription:
    """Idempotent handle returned to each event subscriber."""

    _cancel: Callable[[], None]

    def cancel(self) -> None:
        self._cancel()


class EventStream(Generic[T]):
    """Synchronous fan-out for process-local events.

    Application events deliberately do not create tasks: publication order is
    deterministic, Qt signal adapters can marshal to their own thread, and one
    faulty observer cannot cancel or delay the daemon's task tree beyond its
    own synchronous call. Mutable payloads are copied per observer.
    """

    def __init__(self, name: str, *, copy_payload: bool = True) -> None:
        self._name = name
        self._copy_payload = copy_payload
        self._subscribers: dict[str, Callable[[T], None]] = {}
        self._lock = RLock()

    def subscribe(self, listener: Callable[[T], None]) -> Subscription:
        if not callable(listener):
            raise TypeError("event listener must be callable")
        token = uuid4().hex
        with self._lock:
            self._subscribers[token] = listener

        def _cancel() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return Subscription(_cancel)

    def publish(self, event: T) -> int:
        with self._lock:
            subscribers = tuple(self._subscribers.items())
        delivered = 0
        for token, listener in subscribers:
            try:
                payload = copy.deepcopy(event) if self._copy_payload else event
                listener(payload)
                delivered += 1
            except Exception:
                logger.exception(
                    "Application event listener failed stream=%s subscriber=%s",
                    self._name,
                    token,
                )
        return delivered

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()


@dataclass(frozen=True, slots=True)
class OutboundTransportEvent:
    """Transport-neutral observation of a daemon broadcast."""

    message_type: str
    payload: dict[str, Any]
    correlation_id: str | None = None
    target_client_types: tuple[str, ...] = ()


class ApplicationEventHub:
    """All process-local event streams owned by one application kernel."""

    def __init__(self) -> None:
        self.state: EventStream[dict[str, Any]] = EventStream("state")
        self.intervention: EventStream[dict[str, Any]] = EventStream("intervention")
        self.outbound_transport: EventStream[OutboundTransportEvent] = EventStream(
            "outbound_transport"
        )

    def clear(self) -> None:
        self.state.clear()
        self.intervention.clear()
        self.outbound_transport.clear()
