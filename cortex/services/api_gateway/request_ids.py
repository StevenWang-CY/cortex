"""Bounded validation of client-supplied correlation ids (D16).

Both transport edges accept a caller-chosen correlation id — the HTTP
``X-Cortex-Request-ID`` header and the WebSocket ``correlation_id`` field —
and echo it back on responses, stamp it on every log line and, for WS,
key in-flight request futures on it. Without a bound an unauthenticated
localhost page could push kilobytes of arbitrary bytes (newlines and
control characters included) into every log line and response header of
the request. Anything that does not look like an id is replaced with a
freshly minted one; the client still gets a usable id back.
"""

from __future__ import annotations

import re

MAX_CORRELATION_ID_LENGTH = 128
_CORRELATION_ID_RE = re.compile(rf"^[A-Za-z0-9_.:-]{{1,{MAX_CORRELATION_ID_LENGTH}}}$")


def sanitize_correlation_id(value: object) -> str | None:
    """Return ``value`` when it is a safe, bounded correlation id, else ``None``.

    Accepts the ids Cortex mints itself (``cid_<hex>``, ``ctx_<type>_<n>``)
    and anything a well-behaved client would send (letters, digits and
    ``_ . : -``, at most :data:`MAX_CORRELATION_ID_LENGTH` characters).
    """
    if not isinstance(value, str):
        return None
    if _CORRELATION_ID_RE.fullmatch(value) is None:
        return None
    return value


__all__ = ["MAX_CORRELATION_ID_LENGTH", "sanitize_correlation_id"]
