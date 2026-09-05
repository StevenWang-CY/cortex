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
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_TOKEN_FILENAME = "auth.token"
_TOKEN_BYTES = 32  # 256-bit secret → 64 hex chars.

# D9: ``verify_token`` runs on the event loop for every HTTP request and
# every WS AUTH frame; re-reading the file each time was a blocking
# syscall pair on the hot path. The reader below caches the parsed token
# per path keyed on the file's (inode, mtime_ns, size) signature, so a
# steady-state verification costs one ``stat`` and a rotation (which
# ``os.replace``s a fresh inode into place) is picked up on the very next
# call — the rotation test contract "old token rejected immediately" holds.
_TokenSignature = tuple[int, int, int]
_token_cache: dict[str, tuple[_TokenSignature, str | None]] = {}
_token_cache_lock = threading.Lock()


def _invalidate_token_cache(target: Path) -> None:
    with _token_cache_lock:
        _token_cache.pop(str(target), None)


def _write_token_file(target: Path, token: str) -> None:
    """Persist ``token`` to ``target`` without ever exposing it world-readable.

    D10: the previous implementation used ``write_text`` (which creates
    the file with ``0o666 & ~umask`` — typically 0644) and only then
    ``chmod``-ed it to 0600, leaving a window in which any local user
    could read the secret. The temp file is now created with
    ``os.open(..., O_CREAT | O_EXCL, 0o600)`` so it never exists with a
    wider mode, then atomically ``os.replace``-d over the target. A
    unique temp name means a crashed earlier writer can never make
    ``O_EXCL`` fail permanently, and ``O_NOFOLLOW`` (where available)
    refuses to be redirected through a planted symlink.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Non-POSIX filesystems may reject chmod; on Windows the ACL
            # default is already user-only. Don't fail the daemon over it.
            if sys.platform not in ("win32", "cygwin"):
                logger.warning("Could not set 0600 on %s", tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        _invalidate_token_cache(target)


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

    existing = load_token_or_none(target)
    if existing is not None:
        return existing
    if target.exists():
        logger.warning(
            "Auth token at %s was empty, too short or unreadable; regenerating", target
        )

    token = secrets.token_hex(_TOKEN_BYTES)
    _write_token_file(target, token)
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
    _write_token_file(target, token)
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

    D9: cached per path on the file's ``(st_ino, st_mtime_ns, st_size)``
    signature — one ``stat`` per call in steady state, a re-read only
    when the file actually changed (rotation replaces the inode).
    """
    target = path or auth_token_path()
    key = str(target)
    try:
        st = target.stat()
    except FileNotFoundError:
        _invalidate_token_cache(target)
        return None
    except OSError:
        return None
    signature: _TokenSignature = (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size))
    with _token_cache_lock:
        cached = _token_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    value: str | None = existing if existing and len(existing) >= 32 else None
    with _token_cache_lock:
        _token_cache[key] = (signature, value)
    return value


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
