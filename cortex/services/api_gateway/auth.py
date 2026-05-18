"""FastAPI capability-token dependencies (audit Debt-2).

The Wave-1 fixes F07 (WS SHUTDOWN gate) and F08 (launcher ``/stop`` gate)
shipped tactical per-endpoint checks that consulted
:func:`cortex.libs.auth.verify_token` directly. Debt-2 promotes that
pattern into the FastAPI dependency layer: **every** mutating HTTP route
on the API gateway now requires a capability token, regardless of route.
Adding a new route without an explicit ``Depends(require_capability_token)``
is the only way to bypass auth, which is auditable in code review.

Two dependencies are exported:

``require_capability_token``
    The default. Raises ``HTTPException(401)`` on missing or wrong token
    and emits :data:`EventType.AUTH_REJECTED` with the request path so a
    log aggregator can alarm on spikes (compromised-extension / hostile
    webpage scanning).

``optional_capability_token``
    For ``/health`` only — the supervisor liveness probe must reach the
    daemon without owning the token. Returns ``True``/``False`` so the
    handler can include the auth state in its response if it ever wants
    to (today it does not).

The dependencies accept the token in either ``Authorization: Bearer
<token>`` (canonical) or ``X-Cortex-Auth-Token: <token>`` (legacy /
browser-extension friendlier — Chrome can attach custom headers via
``fetch`` without triggering CORS preflight on the ``X-`` prefix).

Threat model recap (from ``cortex/libs/auth/local_token.py``):
closes the *cross-origin localhost* gap. A hostile webpage in another
browser tab can speak the daemon's protocol but cannot read the mode-0600
auth-token file; the dependency rejects its request with 401 before any
handler runs. The Wave-1 single-endpoint check on the WS ``SHUTDOWN``
message remains as defense-in-depth — see Debt-2 closure docs.
"""

from __future__ import annotations

import logging

from fastapi import Header, HTTPException, Request, status

from cortex.libs.auth import verify_token
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "
_LEGACY_HEADER_NAME = "X-Cortex-Auth-Token"


def _extract_token(
    authorization: str | None,
    x_cortex_auth_token: str | None,
) -> str | None:
    """Pull the candidate token out of either header.

    ``Authorization: Bearer <token>`` is canonical. The legacy
    ``X-Cortex-Auth-Token`` header is accepted because the browser
    extension's ``fetch`` calls are easier to keep CORS-preflight-free
    with an ``X-`` prefix; both must validate against the same token
    file, and either header alone is sufficient.

    Returns ``None`` if neither header carried a usable value so the
    caller can distinguish "no token presented" from "token presented
    but invalid" — both still produce a 401, but the log line names the
    actual failure mode.
    """
    if authorization and authorization.startswith(_BEARER_PREFIX):
        candidate = authorization[len(_BEARER_PREFIX):].strip()
        if candidate:
            return candidate
    if x_cortex_auth_token:
        candidate = x_cortex_auth_token.strip()
        if candidate:
            return candidate
    return None


async def require_capability_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_cortex_auth_token: str | None = Header(default=None, alias=_LEGACY_HEADER_NAME),
) -> str:
    """FastAPI dependency that gates a route on the capability token.

    Raises :class:`fastapi.HTTPException` with status 401 if the token
    is missing or does not match the on-disk value. Returns the token
    on success so a route handler can echo it back into a downstream
    service call (rarely needed — the gateway is the trust boundary).

    The 401 response carries ``WWW-Authenticate: Bearer`` per RFC 7235
    so HTTP-aware clients (e.g. curl with ``--anyauth``) can present
    credentials on retry; the browser extension already attaches the
    token preemptively after Phase E so the retry path is exercised
    only on token rotation.
    """
    token = _extract_token(authorization, x_cortex_auth_token)
    if not token or not verify_token(token):
        path = request.url.path
        reason = "missing" if not token else "invalid"
        # Emit the structured event so log aggregators see auth failures
        # at fixed schema (the cid + path is the indexable join key).
        logger.warning(
            "%s reason=%s path=%s cid=%s",
            EventType.AUTH_REJECTED.value,
            reason,
            path,
            get_correlation_id() or "-",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid capability token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def optional_capability_token(
    authorization: str | None = Header(default=None),
    x_cortex_auth_token: str | None = Header(default=None, alias=_LEGACY_HEADER_NAME),
) -> bool:
    """FastAPI dependency for ``/health`` that never raises.

    Liveness probes must reach the daemon without owning the token —
    a launcher script that boots the daemon, polls ``/health`` until
    healthy, and then hands the token to the UI cannot present a token
    it does not yet have. The probe handler doesn't currently surface
    whether the caller was authenticated; this dependency leaves that
    door open for future telemetry without breaking the liveness gate.
    """
    token = _extract_token(authorization, x_cortex_auth_token)
    if not token:
        return False
    return verify_token(token)


__all__ = [
    "require_capability_token",
    "optional_capability_token",
]
