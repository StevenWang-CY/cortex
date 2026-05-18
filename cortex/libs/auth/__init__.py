"""Local capability tokens (audit F07/F08).

Tactical mitigation for the "any localhost origin is trusted" failure
mode flagged as Architectural Debt #2. The full client-bootstrap rework
is deferred; for Phase 2 we ship a token file that only the daemon's
user can read (mode 0600) and require legitimate callers to present it
on the otherwise destructive endpoints — the WebSocket SHUTDOWN message
and the launcher agent's POST /stop.
"""

from cortex.libs.auth.local_token import (
    AUTH_TOKEN_FILENAME,
    auth_token_path,
    load_or_create_token,
    verify_token,
)

__all__ = [
    "AUTH_TOKEN_FILENAME",
    "auth_token_path",
    "load_or_create_token",
    "verify_token",
]
