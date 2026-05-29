"""
Secret helpers for packaged/local Cortex deployments.

Development can use environment variables and dotenv. Packaged macOS builds can
prefer the system Keychain for API credentials.

Phase-4a hardening (Debt-1)
---------------------------

Two timeout-related defects motivated this rewrite:

* ``security find-generic-password`` is a subprocess; a wedged Keychain
  prompt or a hung TCC daemon could pin the call indefinitely. Bound
  the call with ``timeout=5`` and a ``TimeoutExpired`` catch.
* ``keyring.get_password`` itself can block on user-presence sheets on
  macOS or on a dbus round trip on Linux. Callers in long-lived event
  loops (``cortex.libs.config.settings`` at module import,
  ``cortex.services.llm_engine.anthropic_planner`` at planner
  construction, ``cortex.apps.desktop_shell.onboarding`` during the
  BYOK step) get bitten if the backend stalls. The new
  ``get_password_safe`` runs the call in a thread pool with a hard
  ``future.result(timeout=...)`` so a misbehaving backend at most
  delays startup by ``timeout`` seconds before degrading.
"""

from __future__ import annotations

import concurrent.futures
import logging
import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Default hard-timeout for ``security find-generic-password``. macOS
# normally returns synchronously in <50 ms; anything slower is a sign
# the user is being prompted for a TCC grant or the Keychain is wedged.
_SECURITY_SUBPROCESS_TIMEOUT_S: float = 5.0

# Default hard-timeout for ``keyring.get_password``. Same rationale as
# above — the call is normally instant; anything slower means a
# backend stall and we'd rather degrade than hang.
_KEYRING_DEFAULT_TIMEOUT_S: float = 5.0


# I3: module-level singleton executor. Previously every call created a
# fresh ``ThreadPoolExecutor`` and shut it down with ``wait=False`` on
# timeout, which leaked one OS thread per stall. We now share a single
# worker — keyring operations are inherently serial on macOS (the
# Security framework serialises Keychain access), so one worker is the
# right bound. On timeout we just cancel the future; the worker thread
# eventually unblocks and returns to the pool for the next call.
_KEYRING_EXECUTOR_LOCK = threading.Lock()
_KEYRING_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None

# I3: observability counter for stalled keyring lookups. Surfaced via
# the existing /metrics endpoint by importing this attribute.
_keyring_timeouts_total: int = 0

# Audit fix #20: mirror counter for keyring WRITE timeouts so the BYOK
# onboarding step can detect a stalled backend on its first attempt
# instead of swallowing the stall as a silent "saved" outcome.
_set_password_timeouts_total: int = 0


def _get_keyring_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the process-wide singleton keyring executor (lazy init)."""
    global _KEYRING_EXECUTOR
    if _KEYRING_EXECUTOR is None:
        with _KEYRING_EXECUTOR_LOCK:
            if _KEYRING_EXECUTOR is None:
                _KEYRING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="cortex-keyring",
                )
    return _KEYRING_EXECUTOR


def get_keyring_timeouts_total() -> int:
    """Total count of keyring lookups that exceeded the timeout window.

    Surfaces I3 observability — telemetry / metrics endpoints poll this
    to expose stalled-backend events without parsing logs.
    """
    return _keyring_timeouts_total


def get_set_password_timeouts_total() -> int:
    """Total count of keyring WRITE attempts that exceeded the timeout.

    Mirror of :func:`get_keyring_timeouts_total` for the BYOK path.
    """
    return _set_password_timeouts_total


def get_keychain_password(service: str, account: str) -> str | None:
    """
    Read a generic password from the macOS Keychain.

    Returns None when the secret is missing or Keychain access is unavailable.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                service,
                "-a",
                account,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_SECURITY_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Phase-4a: a wedged TCC prompt / Keychain unlock sheet should
        # degrade to ``None`` (caller falls back to env var) instead of
        # pinning the calling event loop for tens of seconds.
        logger.warning(
            "Keychain lookup for %s/%s timed out after %.1fs; "
            "returning None (caller falls back to env var)",
            service,
            account,
            _SECURITY_SUBPROCESS_TIMEOUT_S,
        )
        return None
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.debug("Keychain lookup failed for %s/%s: %s", service, account, exc)
        return None

    secret = result.stdout.strip()
    return secret or None


def get_password_safe(
    service: str,
    username: str,
    timeout: float = _KEYRING_DEFAULT_TIMEOUT_S,
) -> str | None:
    """Read a generic password via ``keyring`` with a hard wall-clock timeout.

    Wraps ``keyring.get_password`` in a single-worker ``ThreadPoolExecutor``
    so that a stalled backend (macOS unlock sheet, dbus hang, GNOME
    keyring unavailable, …) cannot block the caller for longer than
    ``timeout`` seconds. On timeout we log a warning and return ``None``
    — the caller falls back to env vars / .env values as if the secret
    was simply absent.

    Note: this DOES leak a thread if the backend never returns. We
    accept that trade for liveness — at worst one daemon-lifetime
    thread per stalled backend. The alternative (signal-based timeout)
    is not safe inside an asyncio loop.

    Args:
        service: Keychain service identifier (e.g. ``"cortex.bedrock"``).
        username: Account within that service (e.g. ``"bearer_token"``).
        timeout: Wall-clock seconds before we give up and return ``None``.

    Returns:
        The stored password, or ``None`` if missing / timed out / backend
        unavailable.
    """
    try:
        import keyring  # local import — keyring is an optional dep at runtime
    except ImportError:
        logger.debug("keyring library not installed; treating %s/%s as absent", service, username)
        return None

    def _read() -> str | None:
        try:
            return keyring.get_password(service, username)
        except Exception:
            # ``keyring`` raises both its own ``KeyringError`` family and
            # bare ``RuntimeError`` from broken backends. We treat every
            # exception as "absent" so the caller falls back gracefully.
            logger.debug(
                "keyring.get_password raised for %s/%s",
                service,
                username,
                exc_info=True,
            )
            return None

    # I3: use the module-level singleton executor so we never accumulate
    # threads across calls. Each timeout cancels the future and leaves
    # the worker to drain in the background; subsequent calls reuse the
    # same worker once it returns to idle. Worst case: ``max_workers=1``
    # means a single stalled keyring backend stalls all subsequent
    # lookups until it unblocks, which is preferable to thread leaks
    # because the next caller will hit the same ``timeout`` window and
    # degrade to ``None`` anyway.
    global _keyring_timeouts_total
    executor = _get_keyring_executor()
    future = executor.submit(_read)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        _keyring_timeouts_total += 1
        # C6 (audit): surface the stall on the Prometheus counter the
        # metrics module advertises as a "guaranteed metric". Without this
        # increment the counter shipped a permanent zero. Local import
        # keeps prometheus_client off the hot import path for callers that
        # never touch the keyring.
        from cortex.libs.observability.metrics import KEYRING_TIMEOUTS_TOTAL

        KEYRING_TIMEOUTS_TOTAL.inc()
        logger.warning(
            "keyring.get_password for %s/%s timed out after %.1fs; "
            "returning None (caller falls back to env var) "
            "[total_timeouts=%d]",
            service,
            username,
            timeout,
            _keyring_timeouts_total,
        )
        # Best-effort cancel; the thread may still be blocked in the
        # backend, but it will not deliver its eventual result. The
        # worker remains in the singleton pool — when it eventually
        # unblocks it returns to idle for the next call.
        future.cancel()
        return None


def set_password_safe(
    service: str,
    account: str,
    password: str,
    *,
    timeout: float = _KEYRING_DEFAULT_TIMEOUT_S,
) -> bool:
    """Write a generic password via ``keyring`` with a hard wall-clock timeout.

    Mirror of :func:`get_password_safe` for the write path. Wraps
    ``keyring.set_password`` in the same single-worker
    ``ThreadPoolExecutor`` so a stalled backend (macOS unlock sheet,
    dbus hang, GNOME keyring unavailable, …) cannot block the caller
    for longer than ``timeout`` seconds. On any failure — backend
    missing, timeout, or backend exception — we degrade to ``False``
    so the onboarding step can surface the failure to the user instead
    of pretending the write succeeded.

    Args:
        service: Keychain service identifier (e.g. ``"cortex.bedrock"``).
        account: Account within that service (e.g. ``"bearer_token"``).
        password: Secret value to store.
        timeout: Wall-clock seconds before we give up and return ``False``.

    Returns:
        ``True`` iff ``keyring.set_password`` returned within the
        timeout window without raising; ``False`` otherwise.
    """
    try:
        import keyring  # local import — keyring is an optional dep at runtime
    except ImportError:
        logger.debug(
            "keyring library not installed; cannot write %s/%s",
            service,
            account,
        )
        return False

    def _write() -> bool:
        try:
            keyring.set_password(service, account, password)
            return True
        except Exception:
            logger.debug(
                "keyring.set_password raised for %s/%s",
                service,
                account,
                exc_info=True,
            )
            return False

    global _set_password_timeouts_total
    executor = _get_keyring_executor()
    future = executor.submit(_write)
    try:
        return bool(future.result(timeout=timeout))
    except concurrent.futures.TimeoutError:
        _set_password_timeouts_total += 1
        # C6 (audit): a stalled WRITE is also a Keychain timeout — count it
        # on the same Prometheus counter so /metrics reflects every stall.
        from cortex.libs.observability.metrics import KEYRING_TIMEOUTS_TOTAL

        KEYRING_TIMEOUTS_TOTAL.inc()
        logger.warning(
            "keyring.set_password for %s/%s timed out after %.1fs; "
            "returning False (caller should surface to user) "
            "[total_set_timeouts=%d]",
            service,
            account,
            timeout,
            _set_password_timeouts_total,
        )
        future.cancel()
        return False


__all__ = [
    "get_keychain_password",
    "get_keyring_timeouts_total",
    "get_password_safe",
    "get_set_password_timeouts_total",
    "set_password_safe",
]
