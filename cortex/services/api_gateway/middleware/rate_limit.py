"""Audit F13 — per-route token-bucket rate limiting.

The mutating endpoints (``/state/infer``, ``/llm/plan``,
``/apply_intervention``, ``/shutdown``) each allocate non-trivial server
resources on every call — numpy arrays for inference, Anthropic SDK
requests for planning, file writes for shutdown. A tight-loop client
(buggy extension, hostile localhost web page) could trivially exhaust
memory or rack up LLM cost. The pre-fix gateway had no rate limit at
all.

This middleware implements a 30-line per-IP token bucket in pure Python:

* In-memory ``dict[(ip, route), Deque[float]]`` keyed on client host and
  the matched route prefix.
* Sliding 60-second window. Each request appends ``time.monotonic()``;
  expired entries are popped from the front before the count is checked.
* Per-route caps configured at middleware construction. Routes not in
  the cap table are passed through without limiting (read-only and
  diagnostic endpoints stay open).
* Exceeded requests get a ``429 Too Many Requests`` JSON envelope with a
  ``Retry-After`` header set to the seconds until the next slot frees.

Logging uses :class:`EventType.RATE_LIMITED` with the active correlation
id (F19) so support can quote the cid back to the user when a bounce is
surfaced as a toast.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType

logger = logging.getLogger(__name__)


# Default per-route caps (requests per 60-second window). The legitimate
# UI rarely exceeds these — the extension's hottest endpoint is the WS
# state stream, which does not pass through this middleware.
DEFAULT_LIMITS: dict[str, int] = {
    "/state/infer": 60,
    "/llm/plan": 30,
    "/apply_intervention": 30,
    "/intervention/apply": 30,  # canonical FastAPI path in this repo
    "/shutdown": 5,
}

# Sliding-window size in seconds. 60 s keeps the math simple ("per min")
# and the buckets bounded.
WINDOW_SECONDS: float = 60.0


def _normalise_route(path: str, *, limits: dict[str, int]) -> str | None:
    """Return the limit-table key for *path*, or ``None`` for pass-through.

    The match is prefix-based on the configured keys, so trailing slashes
    or query strings don't accidentally bypass the cap. Returns the
    matched key (so the bucket and the log line agree on the route name).
    """
    for route in limits:
        # exact match or prefix-with-trailing-/ — keeps the table free of
        # regex; FastAPI's path parameters are not used by the gated
        # routes today, so this string match is sufficient.
        if path == route or path.startswith(route + "/"):
            return route
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP, per-route sliding-window rate limiter."""

    def __init__(
        self,
        app,
        *,
        limits: dict[str, int] | None = None,
        window_seconds: float = WINDOW_SECONDS,
        time_func=time.monotonic,
    ) -> None:
        super().__init__(app)
        self._limits: dict[str, int] = dict(limits) if limits is not None else dict(DEFAULT_LIMITS)
        self._window: float = float(window_seconds)
        self._now = time_func
        # ``defaultdict`` over a tuple key keeps the API simple; tests
        # can introspect ``self._buckets`` directly to assert window
        # behaviour without touching private internals.
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    # -- Public surface used by the FastAPI app ---------------------------

    @property
    def limits(self) -> dict[str, int]:
        """Snapshot of the configured per-route caps (read-only view)."""
        return dict(self._limits)

    def reset(self) -> None:
        """Clear all buckets — exposed for test isolation only."""
        self._buckets.clear()

    # -- BaseHTTPMiddleware contract --------------------------------------

    async def dispatch(self, request: Request, call_next) -> Response:
        route = _normalise_route(request.url.path, limits=self._limits)
        if route is None:
            return await call_next(request)

        ip = self._client_ip(request)
        retry_after = self._consume(ip, route)
        if retry_after is None:
            return await call_next(request)

        return self._build_429(route=route, ip=ip, retry_after=retry_after)

    # -- Bucket arithmetic ------------------------------------------------

    def _consume(self, ip: str, route: str) -> int | None:
        """Try to record a hit. Return ``None`` if under cap, else
        the integer ``Retry-After`` value in seconds.
        """
        cap = self._limits[route]
        now = self._now()
        cutoff = now - self._window
        bucket = self._buckets[(ip, route)]

        # Drop entries that fell out of the sliding window.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) < cap:
            bucket.append(now)
            return None

        # Over cap: the next free slot opens when the oldest in-window
        # request expires.
        oldest = bucket[0]
        wait = max(1, int(math.ceil(oldest + self._window - now)))
        return wait

    def _build_429(self, *, route: str, ip: str, retry_after: int) -> JSONResponse:
        cid = get_correlation_id() or "-"
        # Structured log line with the cid so the dashboard's error
        # toasts can quote it back; format mirrors the F10 rejection
        # line so log parsers stay homogeneous.
        logger.warning(
            "%s route=%s ip=%s retry_after=%d cid=%s",
            EventType.RATE_LIMITED.value,
            route,
            ip,
            retry_after,
            cid,
        )
        body = {
            "error": "rate_limited",
            "route": route,
            "retry_after_seconds": retry_after,
            "correlation_id": cid,
        }
        # ``Retry-After`` is an HTTP/1.1 standard header; FastAPI/Starlette
        # surfaces it on the wire unmodified.
        return JSONResponse(
            status_code=429,
            content=body,
            headers={"Retry-After": str(retry_after)},
        )

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    def _client_ip(request: Request) -> str:
        client = request.client
        if client is not None and client.host:
            return client.host
        # ``TestClient`` and some ASGI transports omit ``client``; fall
        # back to the explicit "anonymous" bucket so a single mis-tagged
        # client doesn't escape the limit via missing metadata.
        return "anonymous"


def known_route_paths(limits: dict[str, int] | None = None) -> Iterable[str]:
    """Iterate over the configured route paths (used by introspection)."""
    return tuple((limits or DEFAULT_LIMITS).keys())


__all__ = [
    "RateLimitMiddleware",
    "DEFAULT_LIMITS",
    "WINDOW_SECONDS",
    "known_route_paths",
]
