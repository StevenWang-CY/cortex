"""Reject unauthenticated requests before their body is ever read.

The capability token is enforced as a FastAPI route dependency
(``app.include_router(router, dependencies=[Depends(require_capability_token)])``).
Dependencies run *after* the router has already pumped and parsed the request
body -- ``fastapi/routing.py`` does ``body_bytes = await request.body()`` and
``json_body = await request.json()`` well before ``solve_dependencies``. So an
unauthenticated caller could make the daemon allocate and JSON-parse a body of
any size, repeatedly: the rate limiter deliberately waives budget for
unauthenticated requests (so an anonymous page cannot starve real clients out
of ``/shutdown`` or ``/api/launch``), which means nothing capped the repetition
either. Reproduced against ``create_app()``: an unauthenticated
``POST /state/infer`` with a malformed body answered 422 ``json_invalid`` --
proving the body was read and parsed -- and a 5 MiB body was fully buffered
before the 401 was produced.

This is raw ASGI rather than ``BaseHTTPMiddleware`` on purpose: a
``BaseHTTPMiddleware`` sits above the router but still hands the request on for
the router to read, so the body would be pumped anyway. Answering from
``__call__`` without invoking the inner app means the body is never received.

The route dependency stays in place as defence in depth. This gate is a
pre-filter, not a replacement: it knows nothing about per-route policy.
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cortex.libs.auth import verify_token
from cortex.libs.logging.correlation import get_correlation_id
from cortex.services.api_gateway.auth import extract_token, log_auth_rejected

logger = logging.getLogger(__name__)

# Paths reachable without a capability token. Only the liveness probe: the
# launchers poll it before they own a token, and it is DB-free and cheap.
# Mirrors the ``health_router`` mount in ``app.create_app``.
DEFAULT_PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})

# Hard ceiling on a single request body, applied to the declared
# ``Content-Length`` before anything is read. This is a safety limit, not a
# tuning knob: the largest legitimate payload is a context bundle whose text
# fields are individually capped by the broker's redaction limits, so 2 MiB is
# orders of magnitude above real traffic while still bounding what a buggy or
# hostile authenticated client can make the daemon allocate.
MAX_REQUEST_BODY_BYTES: int = 2 * 1024 * 1024


class CapabilityGateMiddleware:
    """Pre-routing capability-token and body-size gate (pure ASGI)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        public_paths: frozenset[str] | None = None,
        max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self._app = app
        self._public_paths = (
            DEFAULT_PUBLIC_PATHS if public_paths is None else frozenset(public_paths)
        )
        self._max_body_bytes = int(max_body_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        # CORS preflight carries no credentials by design and is answered by
        # CORSMiddleware above this one; 401-ing it would break the browser
        # extension's cross-origin calls.
        if method == "OPTIONS" or path in self._public_paths:
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        token = extract_token(
            headers.get("authorization"), headers.get("x-cortex-auth-token"),
        )
        if not token or not verify_token(token):
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                detail="Missing or invalid capability token",
                extra_headers={"WWW-Authenticate": "Bearer"},
                reason="missing",
                path=path,
            )
            return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length > self._max_body_bytes:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail=(
                        f"Request body exceeds {self._max_body_bytes} bytes"
                    ),
                    extra_headers=None,
                    reason="body_too_large",
                    path=path,
                )
                return

        await self._app(scope, receive, send)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
        extra_headers: dict[str, str] | None,
        reason: str,
        path: str,
    ) -> None:
        cid = get_correlation_id() or "-"
        if status_code == 401:
            # Same structured event, same logger, as the route dependency —
            # aggregators alarm on it and must not care which layer refused.
            log_auth_rejected(reason=reason, path=path)
        else:
            logger.warning(
                "api_gateway_request_rejected reason=%s status=%d path=%s cid=%s",
                reason, status_code, path, cid,
            )
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail, "correlation_id": cid},
            headers=extra_headers,
        )
        # The body is never received: returning here means the inner app --
        # and therefore the router's ``await request.body()`` -- never runs.
        await response(scope, receive, send)


__all__ = [
    "CapabilityGateMiddleware",
    "DEFAULT_PUBLIC_PATHS",
    "MAX_REQUEST_BODY_BYTES",
]
