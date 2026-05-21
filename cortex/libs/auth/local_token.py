"""Local capability token — file-backed shared secret.

Lives at ``<config_dir>/auth.token`` (e.g. on macOS
``~/Library/Application Support/Cortex/auth.token``) with mode 0600 so
only the daemon's user (and root) can read it. Generated lazily the
first time the daemon starts; reused across restarts so legitimate
clients (desktop_shell controller, native-messaging host) can cache it.

Threat model.
-------------
The fix targets the **cross-origin localhost** threat where a malicious
webpage or a hostile extension on the same machine speaks the daemon's
protocol from a browser tab. Neither can read mode-0600 files; both can
speak the protocol. Requiring the token on destructive endpoints
(SHUTDOWN, /stop) closes the gap.

Not in scope: a compromised user account, malware running as the user,
or a debugger attached to the daemon process — those breach any
local-only secret.

Reading the token from a process that does not run as the user
(e.g. a sandboxed extension) requires routing through ``native_host.py``
(see F08). The native host runs as the user and can read the file.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_TOKEN_FILENAME = "auth.token"
_TOKEN_BYTES = 32  # 256-bit secret → 64 hex chars.


def auth_token_path() -> Path:
    """Return the absolute path to the auth-token file.

    Imports ``get_config_dir`` lazily so this module remains usable in
    test contexts that stub out the rest of ``cortex.libs.utils``.
    """
    from cortex.libs.utils.platform import get_config_dir

    return get_config_dir() / AUTH_TOKEN_FILENAME


def load_or_create_token(path: Path | None = None) -> str:
    """Return the persistent capability token, creating it if absent.

    The on-disk format is the raw hex token followed by a trailing
    newline. The file is created mode 0600 atomically (write to a temp
    sibling, ``chmod``, ``rename``) so the legitimate-token-window is
    closed even if the daemon crashes mid-write.

    On Windows, ``os.chmod(0o600)`` is a no-op for ACL semantics; the
    file inherits the user's profile permissions, which already excludes
    other accounts on a default install.
    """
    target = path or auth_token_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8").strip()
            if existing and len(existing) >= 32:
                return existing
            logger.warning(
                "Auth token at %s was empty or too short; regenerating", target
            )
        except OSError as exc:
            logger.warning("Could not read auth token at %s: %s; regenerating", target, exc)

    token = secrets.token_hex(_TOKEN_BYTES)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Non-POSIX filesystems may reject chmod; on Windows the ACL
        # default is already user-only. Don't fail the daemon over it.
        if sys.platform not in ("win32", "cygwin"):
            logger.warning("Could not set 0600 on %s", tmp)
    os.replace(tmp, target)
    logger.info("Generated new Cortex auth token at %s", target)
    return token


def rotate_token(path: Path | None = None) -> str:
    """Replace the on-disk capability token with a freshly-minted one
    and return the new value (audit Debt-2 Commit 5).

    Atomic on POSIX: writes to a sibling ``.tmp`` file mode 0600 first,
    then ``os.replace`` swaps it in. The old token is unrecoverable
    after this call returns; existing clients that present the old
    token will start getting 401 / WS close(1011) until they re-read
    the file (the desktop_shell does this via
    ``WebSocketBridge.refresh_auth_token``; the browser extension does
    this via the native host's ``get_auth_token`` command on next
    connect cycle).

    Idempotency: callers may invoke rotation back-to-back; each call
    returns a fresh token. There is no quota — the threat model
    assumes the user actively chose to rotate.
    """
    target = path or auth_token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(_TOKEN_BYTES)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        if sys.platform not in ("win32", "cygwin"):
            logger.warning("Could not set 0600 on %s", tmp)
    os.replace(tmp, target)
    logger.info("Rotated Cortex auth token at %s", target)
    return token


def load_token_or_none(path: Path | None = None) -> str | None:
    """Read the existing token if present; never mint a new one.

    Audit-prod fix (P1-D): ``verify_token`` previously called
    ``load_or_create_token`` to fetch the comparand, which provisioned
    a fresh token to disk whenever the file was absent. A peer probing
    a daemon mid-rotation (or before first start) could trigger token
    creation. Verification must be side-effect-free; provisioning is
    the daemon ``start()`` path's job.
    """
    target = path or auth_token_path()
    try:
        if not target.exists():
            return None
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not existing or len(existing) < 32:
        return None
    return existing


_DUMMY_TOKEN = "0" * (_TOKEN_BYTES * 2)


def verify_token(presented: str | None, *, path: Path | None = None) -> bool:
    """Constant-time compare of ``presented`` against the stored token.

    Returns ``False`` for any of: a missing/empty presented value, a
    missing/unreadable token file, or a mismatch. Never raises — auth
    failure must be observable but not exploitable as a probe.

    Audit-prod fix (P1-D): pure read; no token is created on miss. Use
    :func:`load_or_create_token` only from the daemon boot path.

    Constant-time-on-miss: when the token file is absent we still
    invoke ``compare_digest`` against a dummy comparand so the
    "no token file" path takes the same wall-clock time as the "wrong
    token" path. Distinguishing the two via response timing would let
    a peer probe daemon lifecycle (pre-first-start vs running) without
    presenting valid credentials.
    """
    if not presented:
        # Equalise the cost of the empty-presented branch with the
        # populated-presented branch so a peer cannot distinguish
        # "client sent nothing" from "client sent wrong" via timing.
        secrets.compare_digest(_DUMMY_TOKEN, _DUMMY_TOKEN)
        return False
    stored = load_token_or_none(path)
    if stored is None:
        secrets.compare_digest(_DUMMY_TOKEN, presented.strip())
        return False
    try:
        return secrets.compare_digest(stored.strip(), presented.strip())
    except Exception:
        return False
